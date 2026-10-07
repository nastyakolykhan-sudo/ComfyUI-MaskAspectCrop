import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "CropRotate";
const ASPECT_RATIOS = { "16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1, "3:4": 3 / 4, "9:16": 9 / 16, "21:9": 21 / 9 };
const FRAME_WIDGETS = ["center_x", "center_y", "crop_width", "crop_height", "rotation"];
const HANDLE_HIT_PX = 10;
const HANDLE_DRAW_PX = 4;
const MIN_FRAME_PX = 8;
const FRAME_COLOR = "#a3e635";
const CURSORS = {
    n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize",
    ne: "nesw-resize", sw: "nesw-resize", nw: "nwse-resize", se: "nwse-resize",
    move: "move", new: "crosshair",
};

const widget = (node, name) => node.widgets?.find((w) => w.name === name);
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));

// Geometry matches nodes.py: the frame (cx, cy, w, h) lives in source pixels and the
// image is rotated by `rotation` degrees (clockwise, y-down) behind the upright frame.
// The editor draws in "display space": the source rotated about the image centre, where
// the frame is axis-aligned and can be dragged with plain rectangle logic.
class CropRotateEditor {
    constructor(node) {
        this.node = node;
        this.image = null;
        this.sourceSize = null;
        this.drag = null;
        this.frozenView = null;
        this.notice = "";

        this.element = document.createElement("div");
        Object.assign(this.element.style, {
            display: "flex", flexDirection: "column", width: "100%", height: "100%", gap: "4px",
        });
        this.canvas = document.createElement("canvas");
        Object.assign(this.canvas.style, {
            flex: "1 1 auto", width: "100%", minHeight: "0", cursor: "crosshair",
            touchAction: "none", borderRadius: "4px",
        });
        this.info = document.createElement("div");
        Object.assign(this.info.style, {
            font: "11px monospace", color: "#aaa", textAlign: "center",
            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        });
        this.element.append(this.canvas, this.info);

        this.canvas.addEventListener("pointerdown", (e) => this.onPointerDown(e));
        this.canvas.addEventListener("pointermove", (e) => this.onPointerMove(e));
        this.canvas.addEventListener("pointerup", (e) => this.onPointerUp(e));
        this.canvas.addEventListener("pointercancel", (e) => this.onPointerUp(e));
        new ResizeObserver(() => this.draw()).observe(this.canvas);
    }

    ready() {
        return !!(this.image && this.sourceSize && widget(this.node, "crop_width"));
    }

    get angleRad() {
        return ((widget(this.node, "rotation")?.value ?? 0) * Math.PI) / 180;
    }

    frameSource() {
        const [W, H] = this.sourceSize;
        const w = widget(this.node, "crop_width").value;
        const h = widget(this.node, "crop_height").value;
        if (!w || !h) return { cx: W / 2, cy: H / 2, w: W, h: H };
        return { cx: widget(this.node, "center_x").value, cy: widget(this.node, "center_y").value, w, h };
    }

    sourceToDisplay(x, y) {
        const [W, H] = this.sourceSize;
        const a = this.angleRad, dx = x - W / 2, dy = y - H / 2;
        return [W / 2 + Math.cos(a) * dx - Math.sin(a) * dy, H / 2 + Math.sin(a) * dx + Math.cos(a) * dy];
    }

    displayToSource(x, y) {
        const [W, H] = this.sourceSize;
        const a = this.angleRad, dx = x - W / 2, dy = y - H / 2;
        return [W / 2 + Math.cos(a) * dx + Math.sin(a) * dy, H / 2 - Math.sin(a) * dx + Math.cos(a) * dy];
    }

    // Bounding box of the rotated image in display space: the frame is dragged within it,
    // so it can reach into the empty corners (filled black by the backend).
    imageBounds() {
        const [W, H] = this.sourceSize;
        const c = Math.abs(Math.cos(this.angleRad)), s = Math.abs(Math.sin(this.angleRad));
        const bw = W * c + H * s, bh = W * s + H * c;
        return { x0: W / 2 - bw / 2, y0: H / 2 - bh / 2, x1: W / 2 + bw / 2, y1: H / 2 + bh / 2 };
    }

    frameDisplay() {
        const f = this.frameSource();
        const [qx, qy] = this.sourceToDisplay(f.cx, f.cy);
        return { x0: qx - f.w / 2, y0: qy - f.h / 2, x1: qx + f.w / 2, y1: qy + f.h / 2 };
    }

    // Frozen during a drag so the view doesn't rescale under the pointer.
    viewBounds() {
        if (this.frozenView) return this.frozenView;
        const b = this.imageBounds(), f = this.frameDisplay();
        return {
            x0: Math.min(b.x0, f.x0), y0: Math.min(b.y0, f.y0),
            x1: Math.max(b.x1, f.x1), y1: Math.max(b.y1, f.y1),
        };
    }

    layout() {
        const cw = this.canvas.clientWidth, ch = this.canvas.clientHeight, pad = 8;
        const v = this.viewBounds();
        const k = Math.min((cw - 2 * pad) / (v.x1 - v.x0), (ch - 2 * pad) / (v.y1 - v.y0));
        return {
            k,
            ox: (cw - (v.x1 - v.x0) * k) / 2 - v.x0 * k,
            oy: (ch - (v.y1 - v.y0) * k) / 2 - v.y0 * k,
        };
    }

    setWidget(name, value) {
        const w = widget(this.node, name);
        if (w) w.value = value;
    }

    writeFrame(r) {
        const x0 = Math.round(r.x0), x1 = Math.round(r.x1), y0 = Math.round(r.y0), y1 = Math.round(r.y1);
        const [cx, cy] = this.displayToSource((x0 + x1) / 2, (y0 + y1) / 2);
        this.setWidget("center_x", Math.round(cx * 100) / 100);
        this.setWidget("center_y", Math.round(cy * 100) / 100);
        this.setWidget("crop_width", Math.max(1, x1 - x0));
        this.setWidget("crop_height", Math.max(1, y1 - y0));
        this.node.setDirtyCanvas(true, true);
    }

    resetFrame() {
        if (!this.sourceSize) return;
        const [W, H] = this.sourceSize;
        this.setWidget("center_x", W / 2);
        this.setWidget("center_y", H / 2);
        this.setWidget("crop_width", W);
        this.setWidget("crop_height", H);
        this.node.setDirtyCanvas(true, true);
    }

    resetAll() {
        this.setWidget("center_x", 0);
        this.setWidget("center_y", 0);
        this.setWidget("crop_width", 0);
        this.setWidget("crop_height", 0);
        this.setWidget("rotation", 0);
        this.setWidget("aspect_ratio", "free");
        this.notice = "";
        this.node.setDirtyCanvas(true, true);
    }

    applyAspect() {
        const ratio = ASPECT_RATIOS[widget(this.node, "aspect_ratio")?.value];
        if (!ratio || !this.ready()) return;
        const f = this.frameDisplay(), b = this.imageBounds();
        let w = Math.sqrt((f.x1 - f.x0) * (f.y1 - f.y0) * ratio), h = w / ratio;
        const s = Math.min(1, (b.x1 - b.x0) / w, (b.y1 - b.y0) / h);
        w *= s;
        h *= s;
        const x0 = clamp((f.x0 + f.x1) / 2 - w / 2, b.x0, b.x1 - w);
        const y0 = clamp((f.y0 + f.y1) / 2 - h / 2, b.y0, b.y1 - h);
        this.writeFrame({ x0, y0, x1: x0 + w, y1: y0 + h });
    }

    pointerPosition(e) {
        const rect = this.canvas.getBoundingClientRect();
        const sx = (e.clientX - rect.left) * (this.canvas.clientWidth / rect.width);
        const sy = (e.clientY - rect.top) * (this.canvas.clientHeight / rect.height);
        const { k, ox, oy } = this.layout();
        return { sx, sy, x: (sx - ox) / k, y: (sy - oy) / k };
    }

    hitTest(p) {
        const { k, ox, oy } = this.layout();
        const f = this.frameDisplay();
        const X0 = f.x0 * k + ox, X1 = f.x1 * k + ox, Y0 = f.y0 * k + oy, Y1 = f.y1 * k + oy;
        const near = (a, b) => Math.abs(a - b) <= HANDLE_HIT_PX;
        const inX = p.sx >= X0 - HANDLE_HIT_PX && p.sx <= X1 + HANDLE_HIT_PX;
        const inY = p.sy >= Y0 - HANDLE_HIT_PX && p.sy <= Y1 + HANDLE_HIT_PX;
        let handle = "";
        if (inX && inY) {
            if (near(p.sy, Y0)) handle += "n";
            else if (near(p.sy, Y1)) handle += "s";
            if (near(p.sx, X0)) handle += "w";
            else if (near(p.sx, X1)) handle += "e";
        }
        if (handle) return handle;
        if (p.sx > X0 && p.sx < X1 && p.sy > Y0 && p.sy < Y1) return "move";
        return "new";
    }

    computeRect(drag, p) {
        const b = this.imageBounds();
        const ratio = ASPECT_RATIOS[widget(this.node, "aspect_ratio")?.value];
        const r = drag.rect;
        const dx = p.x - drag.start.x, dy = p.y - drag.start.y;

        if (drag.mode === "move") {
            const w = r.x1 - r.x0, h = r.y1 - r.y0;
            const x0 = clamp(r.x0 + dx, b.x0, b.x1 - w), y0 = clamp(r.y0 + dy, b.y0, b.y1 - h);
            return { x0, y0, x1: x0 + w, y1: y0 + h };
        }

        if (drag.mode === "new" || drag.mode.length === 2) {
            let ax, ay, px, py;
            if (drag.mode === "new") {
                [ax, ay, px, py] = [drag.start.x, drag.start.y, p.x, p.y];
            } else {
                const west = drag.mode.includes("w"), north = drag.mode.includes("n");
                ax = west ? r.x1 : r.x0;
                ay = north ? r.y1 : r.y0;
                px = (west ? r.x0 : r.x1) + dx;
                py = (north ? r.y0 : r.y1) + dy;
            }
            px = clamp(px, b.x0, b.x1);
            py = clamp(py, b.y0, b.y1);
            let w = Math.abs(px - ax), h = Math.abs(py - ay);
            if (drag.mode === "new" && (w < MIN_FRAME_PX || h < MIN_FRAME_PX)) return null;
            w = Math.max(w, MIN_FRAME_PX);
            h = Math.max(h, MIN_FRAME_PX);
            const sx = px >= ax ? 1 : -1, sy = py >= ay ? 1 : -1;
            if (ratio) {
                if (w / h > ratio) h = w / ratio;
                else w = h * ratio;
                const maxW = sx > 0 ? b.x1 - ax : ax - b.x0;
                const maxH = sy > 0 ? b.y1 - ay : ay - b.y0;
                const s = Math.min(1, maxW / w, maxH / h);
                w *= s;
                h *= s;
            }
            const x0 = sx > 0 ? ax : ax - w, y0 = sy > 0 ? ay : ay - h;
            return { x0, y0, x1: x0 + w, y1: y0 + h };
        }

        let { x0, y0, x1, y1 } = r;
        if (drag.mode === "w" || drag.mode === "e") {
            if (drag.mode === "w") x0 = clamp(r.x0 + dx, b.x0, r.x1 - MIN_FRAME_PX);
            else x1 = clamp(r.x1 + dx, r.x0 + MIN_FRAME_PX, b.x1);
            if (ratio) {
                const cy = (r.y0 + r.y1) / 2;
                const maxH = Math.max(MIN_FRAME_PX, 2 * Math.min(cy - b.y0, b.y1 - cy));
                let w = x1 - x0, h = w / ratio;
                if (h > maxH) {
                    h = maxH;
                    w = h * ratio;
                    if (drag.mode === "w") x0 = x1 - w;
                    else x1 = x0 + w;
                }
                y0 = cy - h / 2;
                y1 = cy + h / 2;
            }
        } else {
            if (drag.mode === "n") y0 = clamp(r.y0 + dy, b.y0, r.y1 - MIN_FRAME_PX);
            else y1 = clamp(r.y1 + dy, r.y0 + MIN_FRAME_PX, b.y1);
            if (ratio) {
                const cx = (r.x0 + r.x1) / 2;
                const maxW = Math.max(MIN_FRAME_PX, 2 * Math.min(cx - b.x0, b.x1 - cx));
                let h = y1 - y0, w = h * ratio;
                if (w > maxW) {
                    w = maxW;
                    h = w / ratio;
                    if (drag.mode === "n") y0 = y1 - h;
                    else y1 = y0 + h;
                }
                x0 = cx - w / 2;
                x1 = cx + w / 2;
            }
        }
        return { x0, y0, x1, y1 };
    }

    onPointerDown(e) {
        if (!this.ready() || e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        this.canvas.setPointerCapture(e.pointerId);
        this.frozenView = this.viewBounds();
        const p = this.pointerPosition(e);
        this.drag = { mode: this.hitTest(p), start: p, rect: this.frameDisplay() };
    }

    onPointerMove(e) {
        if (!this.ready()) return;
        const p = this.pointerPosition(e);
        if (!this.drag) {
            this.canvas.style.cursor = CURSORS[this.hitTest(p)];
            return;
        }
        e.preventDefault();
        e.stopPropagation();
        const rect = this.computeRect(this.drag, p);
        if (rect) {
            this.writeFrame(rect);
            this.draw();
        }
    }

    onPointerUp(e) {
        if (!this.drag) return;
        e.stopPropagation();
        if (this.canvas.hasPointerCapture(e.pointerId)) this.canvas.releasePointerCapture(e.pointerId);
        this.drag = null;
        this.frozenView = null;
        this.draw();
    }

    draw() {
        const c = this.canvas, dpr = window.devicePixelRatio || 1;
        const cw = c.clientWidth, ch = c.clientHeight;
        if (!cw || !ch) return;
        if (c.width !== Math.round(cw * dpr) || c.height !== Math.round(ch * dpr)) {
            c.width = Math.round(cw * dpr);
            c.height = Math.round(ch * dpr);
        }
        const ctx = c.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = "#1b1b1b";
        ctx.fillRect(0, 0, cw, ch);

        if (!this.ready()) {
            ctx.fillStyle = "#888";
            ctx.font = "12px sans-serif";
            ctx.textAlign = "center";
            ctx.fillText("Run the workflow once to load the preview", cw / 2, ch / 2);
            this.info.textContent = "";
            return;
        }

        const { k, ox, oy } = this.layout();
        const [W, H] = this.sourceSize;
        ctx.save();
        ctx.translate((W / 2) * k + ox, (H / 2) * k + oy);
        ctx.rotate(this.angleRad);
        ctx.scale(k, k);
        ctx.drawImage(this.image, -W / 2, -H / 2, W, H);
        ctx.restore();

        const f = this.frameDisplay();
        const X0 = f.x0 * k + ox, Y0 = f.y0 * k + oy, FW = (f.x1 - f.x0) * k, FH = (f.y1 - f.y0) * k;

        ctx.fillStyle = "rgba(0, 0, 0, 0.6)";
        ctx.beginPath();
        ctx.rect(0, 0, cw, ch);
        ctx.rect(X0, Y0, FW, FH);
        ctx.fill("evenodd");

        ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (const t of [1 / 3, 2 / 3]) {
            ctx.moveTo(X0 + FW * t, Y0);
            ctx.lineTo(X0 + FW * t, Y0 + FH);
            ctx.moveTo(X0, Y0 + FH * t);
            ctx.lineTo(X0 + FW, Y0 + FH * t);
        }
        ctx.stroke();

        ctx.strokeStyle = FRAME_COLOR;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(X0, Y0, FW, FH);
        ctx.fillStyle = FRAME_COLOR;
        for (const hx of [X0, X0 + FW / 2, X0 + FW]) {
            for (const hy of [Y0, Y0 + FH / 2, Y0 + FH]) {
                if (hx === X0 + FW / 2 && hy === Y0 + FH / 2) continue;
                ctx.fillRect(hx - HANDLE_DRAW_PX, hy - HANDLE_DRAW_PX, HANDLE_DRAW_PX * 2, HANDLE_DRAW_PX * 2);
            }
        }

        const src = this.frameSource();
        const angle = widget(this.node, "rotation")?.value ?? 0;
        this.info.textContent =
            this.notice || `crop ${src.w}×${src.h}px · ${angle.toFixed(1)}° · source ${W}×${H}`;
    }

    loadPreview(preview) {
        if (!preview?.filename) return;
        const params = new URLSearchParams({
            filename: preview.filename,
            type: preview.type || "temp",
            subfolder: preview.subfolder || "",
            t: String(Date.now()),
        });
        const img = new Image();
        img.onload = () => {
            this.image = img;
            this.draw();
        };
        img.src = api.apiURL(`/view?${params}`);
    }

    onExecuted(message) {
        const size = message?.crop_rotate_source_size;
        if (!size || size.length < 2) return;
        const [W, H] = size;
        const prev = this.sourceSize;
        this.sourceSize = [W, H];
        this.notice = "";
        if (prev && (prev[0] !== W || prev[1] !== H) && widget(this.node, "crop_width")?.value > 0) {
            this.resetFrame();
            this.notice = "Input size changed: crop reset to full image. Run again to apply.";
        }
        const preview = message.crop_rotate_preview?.[0];
        this.node.properties.crop_rotate_source_size = [W, H];
        this.node.properties.crop_rotate_preview = preview;
        this.loadPreview(preview);
    }

    restore() {
        const size = this.node.properties?.crop_rotate_source_size;
        if (size) this.sourceSize = size;
        this.loadPreview(this.node.properties?.crop_rotate_preview);
    }
}

app.registerExtension({
    name: "silverside.CropRotate",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_CLASS) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const editor = new CropRotateEditor(this);
            this.cropRotateEditor = editor;

            for (const name of FRAME_WIDGETS) {
                const w = widget(this, name);
                if (!w) continue;
                const callback = w.callback;
                w.callback = function () {
                    const r = callback?.apply(this, arguments);
                    editor.draw();
                    return r;
                };
            }
            const aspect = widget(this, "aspect_ratio");
            if (aspect) {
                const callback = aspect.callback;
                aspect.callback = function () {
                    const r = callback?.apply(this, arguments);
                    editor.applyAspect();
                    editor.draw();
                    return r;
                };
            }

            const reset = this.addWidget("button", "Reset", null, () => {
                editor.resetAll();
                editor.draw();
            }, { serialize: false });
            reset.serialize = false;

            this.addDOMWidget("crop_rotate_editor", "crop_rotate_editor", editor.element, {
                serialize: false,
                hideOnZoom: false,
                getMinHeight: () => 240,
            });
            this.setSize([Math.max(this.size[0], 380), Math.max(this.size[1], 660)]);
            return result;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            this.cropRotateEditor?.onExecuted(message);
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            this.cropRotateEditor?.restore();
            return r;
        };
    },
});
