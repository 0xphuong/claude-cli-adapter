# Hermes Claude CLI Proxy

Proxy trung gian giữa hermes và `claude` CLI. Hermes gọi API kiểu Anthropic
Messages tới proxy này, proxy gọi `claude -p` (dùng subscription Claude Pro/Max
đã login qua `claude login`) rồi trả kết quả về cho hermes.

## Root cause

Triệu chứng ban đầu: `hermes chat -q "hi"` báo lỗi HTTP 502 qua endpoint
`claude-cli-adapter:8082` (một adapter khác, chạy ở container riêng
172.19.0.5, không có quyền sửa trực tiếp). Có 2 nguyên nhân riêng biệt, cả
hai đều bị nhầm là "hermes gửi request sai format":

1. **Sai tên model.** `config.yaml` cấu hình `default_model: cc/claude-sonnet-5`.
   Adapter bóc tiền tố `cc/` rồi truyền thẳng `claude-sonnet-5` cho
   `claude --model`. CLI/account này không nhận diện được chuỗi
   `claude-sonnet-5` (lỗi "There's an issue with the selected model"), trong
   khi `claude-sonnet-4-6` (hoặc alias `sonnet`) thì chạy được. Xác nhận bằng
   cách gọi trực tiếp `claude -p "hi" --model claude-sonnet-5` (fail) vs
   `--model claude-sonnet-4-6` (pass).

2. **`--system-prompt` tắt cache của Anthropic (nguyên nhân chính gây "out of
   extra usage").** Hermes luôn gửi full system prompt + ~20 tool definitions
   trong MỌI turn, kể cả câu "hi" đơn giản (request thực ~87KB, ~7.800 token).
   Adapter cũ dùng `claude -p --system-prompt "<toàn bộ text>"` cho mỗi lần
   gọi. Cờ `--system-prompt` THAY THẾ system prompt mặc định của Claude Code
   và không được đánh dấu cache_control — verify bằng cách gọi trực tiếp
   `claude -p` với system prompt thật (86KB): `cache_creation_input_tokens`
   luôn bằng 0, tức là toàn bộ nội dung bị tính là input "tươi" mỗi lần, dễ
   vượt quota "extra usage" (overage) vốn gần như bằng 0 của workspace này.
   Request nhỏ (không tools, system ngắn) luôn thành công vì không vượt
   quota — dễ khiến người debug tưởng lầm là do "tools" hay do định dạng
   request, nhưng thực chất là do KÍCH THƯỚC/CHI PHÍ token không được cache.

   Ngược lại, dùng `--append-system-prompt` thay vì `--system-prompt` (chỉ
   BỔ SUNG vào system prompt mặc định, không thay thế) thì Anthropic áp dụng
   cache bình thường — verify: cùng 86KB system prompt, gọi lần đầu tạo
   `cache_creation_input_tokens` lớn (bootstrap), gọi lần 2 với cùng nội dung
   qua `--resume` cùng session thì gần như toàn bộ là `cache_read_input_tokens`
   (rẻ), chỉ vài chục token mới. Route qua CLI **không tự động** né được
   overage như README của repo tham khảo ban đầu (`eliaspfeffer/
   claudehermessubscriptionadapter`) khẳng định — nó chỉ né được NẾU dùng
   đúng cờ cache (`--append-system-prompt`) và tái sử dụng session.

   Lưu ý phụ: lúc debug, việc tôi gọi `claude -p` dồn dập để test cũng tự
   dùng chung quota "extra usage" với hermes (cùng account), khiến kết quả
   test lúc pass lúc fail dù input giống hệt nhau — không phải do nội dung,
   mà do quota là một burst ngắn hạn dùng chung, reset sau ~30s.

## Cách xử lý

1. Sửa `config.yaml`: đổi model từ `claude-sonnet-5` → `claude-sonnet-4-6`
   (cả `model.default` và `providers.anthropic.default_model`).
2. Viết proxy mới (`server.py` trong thư mục này) thay cho
   `claude-cli-adapter` cũ:
   - Dùng `--append-system-prompt` thay vì `--system-prompt` để giữ cache.
   - Duy trì 1 session `claude` bền vững cho mỗi cấu hình (system+tools)
     giống nhau — dùng `--session-id` cho lần đầu, `--resume` cho các lần
     sau, chỉ gửi phần tin nhắn MỚI mỗi lượt thay vì gửi lại toàn bộ.
   - Trả lỗi thật từ CLI về hermes (không nuốt/che lỗi như adapter cũ).
3. Trỏ hermes sang proxy mới: sửa `base_url` trong `config.yaml` và
   `ANTHROPIC_BASE_URL` trong `.env` thành `http://127.0.0.1:8090`, restart
   `hermes gateway`.
4. Verify: gọi `claude -p` trực tiếp với system prompt thật + nhiều tin nhắn
   liên tiếp qua proxy — lần 2 trở đi cache_read tăng vọt, cache_creation gần
   như 0; test nhiều lượt `hermes chat -q "hi"` liên tục đều pass.

Giới hạn còn lại: quota "extra usage" của workspace này thực sự rất mỏng.
Proxy giảm mạnh khả năng bị lỗi (nhờ cache) nhưng không tạo ra quota không
tồn tại — request ĐẦU TIÊN của một cấu hình mới vẫn có thể fail nếu đúng lúc
quota cạn. Muốn hết hẳn thì cần workspace admin tăng "extra usage" trên
console Anthropic.

## Cách chạy (Docker — cách chính)

`server.py` trong thư mục này chính là server mà `Dockerfile` ở thư mục gốc
build vào image. Container lắng nghe cổng **8082** (không phải 8090 — port đó
chỉ là mặc định khi chạy `python server.py` tay; `CMD` truyền port tường minh).

```bash
cd ..                                  # thư mục gốc của repo
docker compose up -d --build           # --build là bắt buộc, xem CLAUDE.md gốc
curl http://127.0.0.1:8082/health
# {"status":"ok","profiles":0}
```

Login cho CLI bên trong container (chỉ cần làm một lần, credential nằm trong
volume `claude-home`):

```bash
docker compose run --rm adapter claude auth login
docker compose run --rm adapter claude auth status
```

Xem log — **không có file log**, service chỉ ghi ra stdout/stderr và Docker giữ
lại (json-file, xoay vòng 3×10MB đã cấu hình trong `docker-compose.yml`):

```bash
docker compose logs -f
```

Xem các session/profile proxy đang giữ (để debug cache reuse):

```bash
curl http://127.0.0.1:8082/debug/profiles
```

Dừng:

```bash
docker compose down
```

## Chạy tay (khi debug, không qua Docker)

```bash
python3 server.py --host 127.0.0.1 --port 8090
```

Chạy foreground và đọc log ngay trên terminal. Đừng redirect ra file — không có
gì trong repo đọc file đó, và một `*.log` bỏ quên trong cây thư mục chỉ tổ lọt
vào build context (đã chặn ở `.dockerignore`/`.gitignore`).

## Yêu cầu

- **Bản Docker:** không cần gì thêm — image đã có sẵn Node + `claude` CLI (pin
  version trong `Dockerfile`) và venv Python.
- **Chạy tay:** `claude` CLI đã cài và đã login (`claude login`) — kiểm tra bằng
  `claude -p "hi"` phải trả lời được, không hỏi login; cùng với `fastapi` +
  `uvicorn`.

## Cấu hình hermes trỏ vào proxy

Port tuỳ cách chạy: **8082** nếu chạy bằng `docker compose` ở repo này, **8090**
nếu chạy tay bằng lệnh ở trên. Các ví dụ dưới dùng bản Docker.

`/opt/data/config.yaml`:

```yaml
model:
  default: claude-sonnet-4-6
  provider: anthropic
providers:
  anthropic:
    api_key: dummy
    base_url: http://127.0.0.1:8082
    default_model: cc/claude-sonnet-4-6
```

`/opt/data/.env`:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:8082
```

Sau khi đổi `.env`/`config.yaml`, restart gateway để áp dụng:

```bash
/opt/hermes/.venv/bin/hermes gateway restart
```

## Lưu ý quan trọng

- Proxy giờ chạy trong container với `restart: unless-stopped`, nên tự lên lại
  sau reboot — khác với giai đoạn đầu khi nó là một tiến trình `nohup` phải
  start tay. Chỉ bản "chạy tay" ở trên mới không được giám sát.
- **Profile giữ trong RAM.** Restart container là mất hết map
  `(system+tools) → session_id`; request kế tiếp của mỗi cấu hình phải
  bootstrap lại (trả full giá đúng một lần). Bản thân session của CLI thì nằm
  trong volume `claude-home` nên không mất.
- **Profile dùng chung giữa các client.** Khoá chỉ là `sha256(system+tools)`.
  Hai hội thoại khác nhau nhưng cùng system prompt sẽ đụng chung một Profile và
  liên tục phá được prefix-check → bootstrap lại mỗi lượt, mất sạch lợi ích
  cache. Với một client duy nhất (hermes) thì không sao; cần chú ý nếu về sau
  có nhiều client cùng trỏ vào.
- Nếu đổi model trong `config.yaml`, phải là tên `claude` CLI nhận được
  (ví dụ `claude-sonnet-4-6`, alias `sonnet`) — không phải mọi tên model mới
  nhất đều được CLI/account này chấp nhận. Test trước bằng:
  `claude -p "hi" --model <tên-model>`.
- Quota "extra usage" của account này rất mỏng (burst ngắn hạn, reset sau
  ~30s). Proxy giảm mạnh khả năng bị lỗi nhờ cache, nhưng không thể tạo ra
  quota không tồn tại — request đầu tiên của một cấu hình (system+tools) mới
  vẫn có thể fail nếu đúng lúc quota cạn.
