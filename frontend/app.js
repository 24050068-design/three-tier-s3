// URL API mà trình duyệt gọi. Mặc định /api (đi qua nginx proxy sang backend).
// Muốn gọi thẳng backend thì đặt window.API_URL trước khi nạp file này.
const API_URL = (window.API_URL || "/api").replace(/\/+$/, "");
const API = `${API_URL}/todos`;

let todos = [];
let loading = true;
let editingId = null;

const $ = (id) => document.getElementById(id);
const listEl = $("list");
const errorEl = $("error");
const addForm = $("add-form");
const addInput = $("add-input");

const addImage = $("add-image");
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

// key ảnh trên S3 -> URL ký sẵn mới nhất (cập nhật mỗi lần hỏi /api/images).
const imageUrls = new Map();

async function request(url, options = {}) {
  // FormData (upload ảnh): để trình duyệt tự đặt Content-Type kèm boundary.
  const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const res = await fetch(url, { headers, ...options });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = ": " + body.detail;
    } catch {}
    throw new Error(`Lỗi ${res.status}${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

function setError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = !msg;
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const c of children) node.append(c);
  return node;
}

function render() {
  $("loading").hidden = !loading;
  $("empty").hidden = loading || todos.length > 0 || !errorEl.hidden;

  let editInput = null;
  listEl.replaceChildren(
    ...todos.map((t) => {
      const li = el("li", { className: t.done ? "done" : "" });

      if (editingId === t.id) {
        const input = el("input", { value: t.title, maxLength: 255 });
        input.addEventListener("keydown", (e) => e.key === "Escape" && cancelEdit());
        const form = el("form", { className: "edit" }, [
          input,
          el("button", { type: "submit", textContent: "Lưu" }),
          el("button", {
            type: "button",
            className: "secondary",
            textContent: "Hủy",
            onclick: cancelEdit,
          }),
        ]);
        form.addEventListener("submit", (e) => {
          e.preventDefault();
          saveEdit(t, input.value);
        });
        li.append(form);
        editInput = input;
        return li;
      }

      const checkbox = el("input", { type: "checkbox", checked: t.done, onchange: () => toggle(t) });
      const span = el("span", { textContent: t.title, ondblclick: () => startEdit(t) });
      // Ảnh minh chứng: chỉ hiện khi ảnh còn trên S3 (xoá trên S3 thì ở đây cũng mất).
      const proofUrl = t.image_key && imageUrls.get(t.image_key);
      const proof = proofUrl
        ? [
            el("a", { href: proofUrl, target: "_blank", rel: "noopener", className: "proof", title: "Xem ảnh minh chứng" }, [
              el("img", { src: proofUrl, alt: "Minh chứng: " + t.title, loading: "lazy" }),
            ]),
          ]
        : [];
      li.append(
        el("label", {}, [checkbox, span]),
        ...proof,
        el("div", { className: "actions" }, [
          el("button", {
            className: t.done ? "secondary" : "ok",
            textContent: t.done ? "Bỏ done" : "Done",
            onclick: () => toggle(t),
          }),
          el("button", { className: "secondary", textContent: "Sửa", onclick: () => startEdit(t) }),
          el("button", { className: "del", textContent: "Xóa", onclick: () => remove(t.id) }),
        ])
      );
      return li;
    })
  );
  if (editInput) editInput.focus();
}

async function load() {
  try {
    setError("");
    todos = await request(API);
  } catch (e) {
    setError("Không tải được dữ liệu. Backend đã chạy chưa? (" + e.message + ")");
  } finally {
    loading = false;
    render();
  }
}

addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = addInput.value.trim();
  if (!text) return;
  const body = new FormData();
  body.append("title", text);
  const file = addImage.files[0];
  if (file) body.append("image", file);
  $("add-btn").disabled = true;
  try {
    setError("");
    const created = await request(API, { method: "POST", body });
    todos = [created, ...todos];
    addInput.value = "";
    clearPickedImage();
    render();
    if (created.image_key) loadImages(); // lấy URL ảnh vừa tải lên để hiện ngay
  } catch (e) {
    setError(e.message);
  } finally {
    $("add-btn").disabled = false;
  }
});

function clearPickedImage() {
  addImage.value = "";
  $("add-image-name").textContent = "Chưa chọn ảnh (tối đa 5 MB)";
  $("add-image-clear").hidden = true;
}

addImage.addEventListener("change", () => {
  const file = addImage.files[0];
  if (!file) return clearPickedImage();
  if (file.size > MAX_IMAGE_BYTES) {
    clearPickedImage();
    return setError("Ảnh vượt quá 5 MB, hãy chọn ảnh nhỏ hơn.");
  }
  setError("");
  $("add-image-name").textContent = file.name;
  $("add-image-clear").hidden = false;
});

$("add-image-clear").addEventListener("click", clearPickedImage);

async function update(id, changes) {
  try {
    const updated = await request(`${API}/${id}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
    todos = todos.map((t) => (t.id === id ? updated : t));
    render();
    return true;
  } catch (e) {
    setError(e.message);
    render();
    return false;
  }
}

function toggle(todo) {
  update(todo.id, { done: !todo.done });
}

function startEdit(todo) {
  editingId = todo.id;
  render();
}

function cancelEdit() {
  editingId = null;
  render();
}

async function saveEdit(todo, value) {
  const text = value.trim();
  if (!text) return;
  if (text === todo.title || (await update(todo.id, { title: text }))) {
    cancelEdit();
  }
}

async function remove(id) {
  try {
    await request(`${API}/${id}`, { method: "DELETE" });
    todos = todos.filter((t) => t.id !== id);
    render();
  } catch (e) {
    setError(e.message);
  }
}

// Ảnh từ S3: backend trả [{key, name, url}] theo đúng những gì đang có trong bucket.
// Hỏi lại định kỳ và so theo key: ảnh bị xoá trên S3 thì gỡ khỏi trang, ảnh mới thì thêm vào,
// ảnh cũ giữ nguyên thẻ <img> (URL ký sẵn đổi mỗi lần gọi, thay src sẽ làm ảnh tải lại/nháy).
const IMAGE_POLL_MS = 5000;
const galleryEl = $("gallery");
const shown = new Map(); // key -> <figure>

function syncImages(images) {
  const keys = new Set(images.map((i) => i.key));
  // Tập ảnh thay đổi (thêm/xoá trên S3) thì vẽ lại danh sách công việc để cập nhật ảnh minh chứng.
  const changed = keys.size !== imageUrls.size || images.some((i) => !imageUrls.has(i.key));
  imageUrls.clear();
  for (const img of images) imageUrls.set(img.key, img.url);
  if (changed && editingId === null) render();

  for (const [key, node] of shown) {
    if (!keys.has(key)) {
      node.remove();
      shown.delete(key);
    }
  }
  for (const img of images) {
    if (shown.has(img.key)) continue;
    const node = el("figure", {}, [
      el("img", { src: img.url, alt: img.name, loading: "lazy" }),
      el("figcaption", { textContent: img.name }),
    ]);
    shown.set(img.key, node);
    galleryEl.append(node);
  }
  $("gallery-title").textContent = `Ảnh từ S3 (${images.length})`;
  $("gallery-empty").hidden = images.length > 0;
}

async function loadImages() {
  try {
    syncImages(await request(`${API_URL}/images`));
    $("gallery-error").hidden = true;
  } catch (e) {
    // Lỗi tạm thời thì giữ nguyên ảnh đang hiển thị, không ảnh hưởng todo.
    $("gallery-error").textContent = "Không tải được danh sách ảnh (" + e.message + ")";
    $("gallery-error").hidden = false;
  }
}

render();
load();
loadImages();
setInterval(() => document.hidden || loadImages(), IMAGE_POLL_MS);
document.addEventListener("visibilitychange", () => document.hidden || loadImages());
