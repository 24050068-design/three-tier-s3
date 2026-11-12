# Frontend là HTML/CSS/JS thuần: không cần Node, nginx phục vụ trực tiếp, proxy /api sang backend
FROM nginx:1.27-alpine
# backend-url.sh chạy lúc khởi động: chuẩn hoá BACKEND_URL (từ .env) rồi điền vào nginx.conf.
COPY nginx.conf /etc/nginx/backend.conf.template
COPY backend-url.sh /docker-entrypoint.d/15-backend-url.sh
# Bỏ CRLF (file sửa trên Windows) để sh chạy được.
RUN sed -i 's/\r$//' /docker-entrypoint.d/15-backend-url.sh && chmod +x /docker-entrypoint.d/15-backend-url.sh
ENV BACKEND_URL=http://backend:8000
COPY index.html style.css app.js /usr/share/nginx/html/
EXPOSE 80
