#!/bin/sh
# Chạy mỗi lần container khởi động: chuẩn hoá BACKEND_URL (từ frontend/.env) rồi sinh cấu hình nginx.
set -e

raw="${BACKEND_URL:-http://backend:8000}"
url="$raw"

# Thiếu http:// thì thêm vào.
case "$url" in
  http://*|https://*) ;;
  *) url="http://$url" ;;
esac

scheme="${url%%://*}"
hostport="${url#*://}"
hostport="${hostport%%/*}"   # bỏ phần đường dẫn như / hoặc /api ở cuối
host="${hostport%:*}"
port=""
[ "$host" != "$hostport" ] && port="${hostport##*:}"

# 127.0.0.1/localhost trong container là chính container nginx, nên đổi sang máy host.
case "$host" in
  127.0.0.1|localhost|0.0.0.0)
    host="host.docker.internal"
    # Docker DNS không trả host.docker.internal trên Linux (dùng extra_hosts), nên lấy luôn IP.
    ip="$(getent hosts host.docker.internal | awk '{print $1; exit}')"
    [ -n "$ip" ] && host="$ip"
    ;;
esac

[ -z "$port" ] && { [ "$scheme" = https ] && port=443 || port=80; }
BACKEND_URL="$scheme://$host:$port"
export BACKEND_URL

envsubst '${BACKEND_URL}' < /etc/nginx/backend.conf.template > /etc/nginx/conf.d/default.conf
echo "frontend: /api -> $BACKEND_URL (BACKEND_URL trong .env: $raw)"

# Chỉ kiểm tra để báo trong log, không chặn khởi động (backend có thể lên sau).
if wget -q -T 3 -O /dev/null "$BACKEND_URL/api/todos" 2>/dev/null; then
  echo "frontend: kết nối backend OK"
else
  echo "frontend: CẢNH BÁO - chưa gọi được $BACKEND_URL/api/todos (backend chưa chạy hoặc sai địa chỉ/cổng trong frontend/.env)"
fi
