import os
import time
import uuid
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine, func, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# Đọc backend/.env khi chạy `python main.py`.
# Không ghi đè biến đã có sẵn (vd. biến do docker-compose truyền vào qua env_file).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "123456")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "todo_db")

# S3 (tuỳ chọn): để trống S3_BUCKET thì /api/images trả về danh sách rỗng.
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_PREFIX = os.getenv("S3_PREFIX", "anh/")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
# URL của dịch vụ S3 mà backend gọi tới. Dùng endpoint theo region: endpoint toàn cầu
# trả 307 redirect làm sai chữ ký của URL ký sẵn.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL") or f"https://s3.{AWS_REGION}.amazonaws.com"

# Ảnh minh chứng đính kèm công việc: loại file cho phép và dung lượng tối đa.
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Todo(Base):
    __tablename__ = "todos"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    done = Column(Boolean, nullable=False, default=False)
    # Key của ảnh minh chứng trên S3 (nếu có).
    image_key = Column(String(512), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class TodoUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    done: Optional[bool] = None


class TodoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    done: bool
    image_key: Optional[str] = None
    created_at: datetime


def ensure_database() -> None:
    """Tạo database DB_NAME nếu chưa có (vd. RDS tạo mà không điền Initial database name)."""
    # Kết nối tới máy chủ MySQL, không chọn database nào.
    server = create_engine(
        f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/?charset=utf8mb4"
    )
    name = DB_NAME.replace("`", "``")
    try:
        with server.begin() as conn:
            conn.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            )
    except OperationalError as exc:
        # 1044: user không có quyền tạo database. Bỏ qua, vì database có thể đã được tạo sẵn.
        if exc.orig.args and exc.orig.args[0] == 1044:
            print(f"User {DB_USER} không có quyền tạo database {DB_NAME}, dùng database có sẵn.")
        else:
            raise
    finally:
        server.dispose()


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    """Chờ MySQL sẵn sàng, tạo database và bảng (nếu chưa có)."""
    for attempt in range(1, retries + 1):
        try:
            ensure_database()
            Base.metadata.create_all(bind=engine)
            # Bảng tạo từ bản cũ chưa có cột image_key thì thêm vào.
            if "image_key" not in {c["name"] for c in inspect(engine).get_columns("todos")}:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE todos ADD COLUMN image_key VARCHAR(512) NULL"))
            print("Đã kết nối MySQL.")
            return
        except OperationalError as exc:
            print(f"[{attempt}/{retries}] Chưa kết nối được MySQL: {exc.orig}")
            time.sleep(delay)
    raise RuntimeError("Không kết nối được MySQL, kiểm tra docker compose / biến môi trường.")


init_db()

app = FastAPI(title="Todo API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # VS Code Live Server
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}


_s3 = None


def s3_client():
    """Tạo S3 client một lần (frontend gọi /api/images vài giây một lần)."""
    global _s3
    if _s3 is None:
        import boto3  # chỉ nạp khi dùng S3
        from botocore.config import Config

        # Quyền lấy từ IAM Role gắn vào EC2, không cần access key.
        # Bucket, thư mục, region và URL S3 đều lấy từ .env.
        _s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
            endpoint_url=S3_ENDPOINT_URL,
            config=Config(signature_version="s3v4"),
        )
    return _s3


@app.get("/api/images")
def list_images():
    """Liệt kê ảnh đang có trong s3://S3_BUCKET/S3_PREFIX.

    Mỗi lần gọi đều hỏi lại S3 nên ảnh bị xoá trên S3 sẽ không còn trong kết quả.
    Trả về [{key, name, url}], url là URL ký sẵn (hết hạn sau 1 giờ).
    """
    if not S3_BUCKET:
        return []
    s3 = s3_client()
    images = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
        for o in page.get("Contents", []):
            key = o["Key"]
            if not key.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                continue
            images.append(
                {
                    "key": key,
                    "name": key.rsplit("/", 1)[-1],
                    "url": s3.generate_presigned_url(
                        "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=3600
                    ),
                }
            )
    return images


@app.get("/api/todos", response_model=List[TodoOut])
def list_todos(db: Session = Depends(get_db)):
    return db.query(Todo).order_by(Todo.id.desc()).all()


def upload_image(image: UploadFile) -> str:
    """Lưu ảnh minh chứng lên S3, trả về key."""
    if not S3_BUCKET:
        raise HTTPException(status_code=400, detail="Chưa cấu hình S3_BUCKET nên không đính kèm ảnh được")
    ext = IMAGE_TYPES.get(image.content_type or "")
    if ext is None:
        raise HTTPException(status_code=400, detail="Chỉ nhận ảnh PNG, JPG, GIF hoặc WEBP")
    data = image.file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Ảnh vượt quá 5 MB")
    if not data:
        raise HTTPException(status_code=400, detail="File ảnh rỗng")
    key = f"{S3_PREFIX}{uuid.uuid4().hex}{ext}"
    s3_client().put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=image.content_type)
    return key


@app.post("/api/todos", response_model=TodoOut, status_code=201)
def create_todo(
    title: str = Form(min_length=1, max_length=255),
    image: Optional[UploadFile] = File(default=None),
    db: Session = Depends(get_db),
):
    """Tạo công việc (multipart/form-data), kèm ảnh minh chứng nếu có."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Tiêu đề không được để trống")
    image_key = upload_image(image) if image is not None and image.filename else None
    todo = Todo(title=title, done=False, image_key=image_key)
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return todo


@app.patch("/api/todos/{todo_id}", response_model=TodoOut)
def update_todo(todo_id: int, payload: TodoUpdate, db: Session = Depends(get_db)):
    todo = db.get(Todo, todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy todo")
    if payload.title is not None:
        todo.title = payload.title.strip()
    if payload.done is not None:
        todo.done = payload.done
    db.commit()
    db.refresh(todo)
    return todo


@app.delete("/api/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int, db: Session = Depends(get_db)):
    todo = db.get(Todo, todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy todo")
    image_key = todo.image_key
    db.delete(todo)
    db.commit()
    # Xoá công việc thì xoá luôn ảnh minh chứng trên S3 (lỗi S3 không làm hỏng việc xoá).
    if image_key and S3_BUCKET:
        try:
            s3_client().delete_object(Bucket=S3_BUCKET, Key=image_key)
        except Exception as exc:
            print(f"Không xoá được ảnh {image_key} trên S3: {exc}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")), reload=True)
