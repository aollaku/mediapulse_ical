import io
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pymupdf as fitz
import numpy as np
from PIL import Image
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from icalendar import Calendar, Event
from werkzeug.utils import secure_filename

try:
    import caldav
except Exception:
    caldav = None

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-please")

DEFAULT_CALENDAR_NAMES = {
    "work": "MediaPulse Work",
    "ot": "MediaPulse Overtime",
    "unavailable": "MediaPulse Unavailable/Leave",
    "off": "MediaPulse Off",
}

DEFAULT_PALETTE = {
    "work": (197, 229, 184),
    "ot": (230, 150, 150),
    "unavailable": (190, 220, 245),
    "off": (240, 240, 240),
}

LABEL_TITLES = {
    "work": "Work",
    "ot": "Overtime",
    "unavailable": "Unavailable / Leave",
    "off": "Off",
}


def hex_to_rgb(hex_str: str):
    value = hex_str.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("Hex color must be 6 characters.")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def crop_with_bounds(img: Image.Image, box):
    left, top, right, bottom = box
    left = max(0, int(left))
    top = max(0, int(top))
    right = min(img.width, int(right))
    bottom = min(img.height, int(bottom))
    return img.crop((left, top, right, bottom))


def render_pdf_first_page(pdf_path: Path) -> Path:
    doc = fitz.open(pdf_path)
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    img_path = UPLOAD_DIR / f"{pdf_path.stem}_page1.png"
    pix.save(img_path)
    return img_path


def rgb_to_hsv_np(rgb):
    rgb = rgb / 255.0
    r = rgb[:, 0]
    g = rgb[:, 1]
    b = rgb[:, 2]

    mx = np.max(rgb, axis=1)
    mn = np.min(rgb, axis=1)
    diff = mx - mn

    h = np.zeros_like(mx)

    mask = diff != 0
    rmask = mask & (mx == r)
    gmask = mask & (mx == g)
    bmask = mask & (mx == b)

    h[rmask] = ((g[rmask] - b[rmask]) / diff[rmask]) % 6
    h[gmask] = ((b[gmask] - r[gmask]) / diff[gmask]) + 2
    h[bmask] = ((r[bmask] - g[bmask]) / diff[bmask]) + 4
    h = h / 6.0

    s = np.zeros_like(mx)
    nz = mx != 0
    s[nz] = diff[nz] / mx[nz]

    v = mx
    return h, s, v


def classify_cell(cell_img: Image.Image):
    """
    Rules:
    - white/gray/yellow outer + green inner => work
    - white/gray/yellow outer + red inner   => ot
    - white/gray/yellow outer + blue inner  => unavailable
    - solid white only                      => off
    - solid gray only                       => off
    - solid yellow only                     => off
    """

    arr = np.asarray(cell_img.convert("RGB")).astype(np.float32)

    # Ignore borders and inspect inner portion of cell
    h, w, _ = arr.shape
    x1 = int(w * 0.12)
    x2 = int(w * 0.88)
    y1 = int(h * 0.12)
    y2 = int(h * 0.88)
    inner = arr[y1:y2, x1:x2]

    pixels = inner.reshape((-1, 3))
    mean_rgb = tuple(int(x) for x in pixels.mean(axis=0))

    hsv_h, hsv_s, hsv_v = rgb_to_hsv_np(pixels)

    # Background-like pixels
    white_like = (hsv_v > 0.93) & (hsv_s < 0.08)
    gray_like = (hsv_v > 0.45) & (hsv_v < 0.93) & (hsv_s < 0.10)
    yellow_bg_like = (
        (hsv_h >= 35 / 360) &
        (hsv_h <= 60 / 360) &
        (hsv_s >= 0.10) &
        (hsv_s < 0.35) &
        (hsv_v > 0.75)
    )

    background_mask = white_like | gray_like | yellow_bg_like
    active = pixels[~background_mask]

    coverage = len(active) / len(pixels) if len(pixels) else 0.0

    # No meaningful inner color => off
    if len(active) == 0 or coverage < 0.015:
        return "off", mean_rgb, {"coverage": round(coverage, 3), "reason": "white/gray/yellow off"}

    ah, as_, av = rgb_to_hsv_np(active)

    green_mask = (ah >= 70 / 360) & (ah <= 150 / 360) & (as_ > 0.12)
    red_mask = (((ah >= 0 / 360) & (ah <= 20 / 360)) | ((ah >= 340 / 360) & (ah <= 1.0))) & (as_ > 0.12)
    blue_mask = (ah >= 170 / 360) & (ah <= 250 / 360) & (as_ > 0.12)

    green_ratio = np.sum(green_mask) / len(active)
    red_ratio = np.sum(red_mask) / len(active)
    blue_ratio = np.sum(blue_mask) / len(active)

    if green_ratio >= 0.18:
        return "work", mean_rgb, {"coverage": round(coverage, 3), "reason": "green detected"}

    if red_ratio >= 0.18:
        return "ot", mean_rgb, {"coverage": round(coverage, 3), "reason": "red detected"}

    if blue_ratio >= 0.18:
        return "unavailable", mean_rgb, {"coverage": round(coverage, 3), "reason": "blue detected"}

    dominant = max(
        [("work", green_ratio), ("ot", red_ratio), ("unavailable", blue_ratio)],
        key=lambda x: x[1]
    )

    if dominant[1] >= 0.08:
        weak_reasons = {
            "work": "green weak",
            "ot": "red weak",
            "unavailable": "blue weak",
        }
        return dominant[0], mean_rgb, {"coverage": round(coverage, 3), "reason": weak_reasons[dominant[0]]}

    return "off", mean_rgb, {"coverage": round(coverage, 3), "reason": "no strong inner color"}


def build_events_from_selection(
    image_path: Path,
    month_start: date,
    row_box,
    first_day_x,
    last_day_x,
    day_count,
    employee_name=""
):
    img = Image.open(image_path)
    row_img = crop_with_bounds(img, row_box)

    x0 = float(first_day_x)
    x1 = float(last_day_x)
    if x1 <= x0:
        raise ValueError("Last day marker must be to the right of first day marker.")

    cell_width = (x1 - x0) / max(day_count - 1, 1)
    row_left = row_box[0]
    rel_first = x0 - row_left
    day_entries = []

    for idx in range(day_count):
        cell_center_x = rel_first + idx * cell_width

        left = cell_center_x - (cell_width * 0.40)
        right = cell_center_x + (cell_width * 0.40)
        top = row_img.height * 0.12
        bottom = row_img.height * 0.88

        cell_img = crop_with_bounds(row_img, (left, top, right, bottom))
        label, rgb, debug = classify_cell(cell_img)
        event_date = month_start + timedelta(days=idx)

        day_entries.append({
            "date": event_date.isoformat(),
            "label": label,
            "rgb": list(rgb),
            "title": LABEL_TITLES[label],
            "employee_name": employee_name or "Roster",
            "coverage": debug["coverage"],
            "reason": debug["reason"],
        })

    return day_entries


def make_ical(events, label=None):
    cal = Calendar()
    cal.add("prodid", "-//MediaPulse iCal Sync//OpenAI//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    for entry in events:
        if label and entry["label"] != label:
            continue

        ev = Event()
        start = date.fromisoformat(entry["date"])
        ev.add("uid", f"{uuid.uuid4()}@mediapulse-local")
        ev.add("dtstamp", datetime.utcnow())
        ev.add("dtstart", start)
        ev.add("dtend", start + timedelta(days=1))
        ev.add("summary", entry.get("title") or LABEL_TITLES[entry["label"]])
        ev.add("description", f"Imported from MediaPulse PDF. Type: {entry['label']}")
        ev.add("categories", entry["label"])
        ev.add("X-MEDIAPULSE-AUTO", "1")
        cal.add_component(ev)

    return cal.to_ical()


def ensure_calendar(principal, name):
    for cal in principal.calendars():
        try:
            if cal.name == name:
                return cal
        except Exception:
            pass
    return principal.make_calendar(name=name)


def daterange_event_bounds(events):
    if not events:
        today = date.today()
        return today, today + timedelta(days=1)

    start = min(date.fromisoformat(e["date"]) for e in events)
    end = max(date.fromisoformat(e["date"]) for e in events) + timedelta(days=1)
    return start, end


def is_mediapulse_auto_event(raw: str) -> bool:
    return "X-MEDIAPULSE-AUTO:1" in raw or "Imported from MediaPulse PDF" in raw


def delete_existing_event_for_date_across_all_target_calendars(principal, calendar_names, target_date):
    start = target_date
    end = target_date + timedelta(days=1)

    for _, cal_name in calendar_names.items():
        cal = ensure_calendar(principal, cal_name)
        try:
            for found in cal.date_search(start, end):
                raw = found.data
                if is_mediapulse_auto_event(raw):
                    found.delete()
        except Exception:
            pass


def clear_existing_events_across_all_target_calendars(principal, calendar_names, start, end):
    for _, cal_name in calendar_names.items():
        cal = ensure_calendar(principal, cal_name)
        try:
            for found in cal.date_search(start, end):
                raw = found.data
                if is_mediapulse_auto_event(raw):
                    found.delete()
        except Exception:
            pass


def sync_to_icloud(events, url, username, password, calendar_names, replace_existing=False):
    if caldav is None:
        raise RuntimeError("caldav library is not installed.")

    client = caldav.DAVClient(url=url, username=username, password=password)
    principal = client.principal()

    start, end = daterange_event_bounds(events)

    if replace_existing:
        clear_existing_events_across_all_target_calendars(
            principal=principal,
            calendar_names=calendar_names,
            start=start,
            end=end,
        )

    grouped = {k: [] for k in calendar_names}
    for entry in events:
        entry_date = date.fromisoformat(entry["date"])
        delete_existing_event_for_date_across_all_target_calendars(
            principal=principal,
            calendar_names=calendar_names,
            target_date=entry_date,
        )
        grouped[entry["label"]].append(entry)

    results = []
    for label, items in grouped.items():
        if not items:
            continue

        cal = ensure_calendar(principal, calendar_names[label])

        for entry in items:
            cal.add_event(make_ical([entry]).decode("utf-8"))

        results.append((label, len(items), calendar_names[label]))

    return results


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        pdf_file = request.files.get("pdf_file")
        if not pdf_file or not pdf_file.filename.lower().endswith(".pdf"):
            flash("Please upload a PDF file.")
            return redirect(url_for("index"))

        try:
            month_start = date.fromisoformat(request.form.get("month_start", "").strip())
            day_count = int(request.form.get("day_count", "").strip())
            if day_count < 1 or day_count > 31:
                raise ValueError
        except Exception:
            flash("Enter a valid month start date and day count.")
            return redirect(url_for("index"))

        employee_name = request.form.get("employee_name", "").strip()
        prefix = request.form.get("calendar_prefix", "").strip() or "MediaPulse"

        filename = secure_filename(pdf_file.filename)
        pdf_path = UPLOAD_DIR / filename
        pdf_file.save(pdf_path)
        image_path = render_pdf_first_page(pdf_path)

        session["image_path"] = str(image_path)
        session["month_start"] = month_start.isoformat()
        session["day_count"] = day_count
        session["employee_name"] = employee_name
        session["calendar_names"] = {
            "work": f"{prefix} Work",
            "ot": f"{prefix} Overtime",
            "unavailable": f"{prefix} Unavailable/Leave",
            "off": f"{prefix} Off",
        }

        palette = {}
        for label in ("work", "ot", "unavailable", "off"):
            raw = request.form.get(f"color_{label}", "").strip()
            palette[label] = hex_to_rgb(raw) if raw else DEFAULT_PALETTE[label]
        session["palette"] = {k: list(v) for k, v in palette.items()}

        return redirect(url_for("select"))

    return render_template("index.html", palette=DEFAULT_PALETTE)


@app.route("/select", methods=["GET", "POST"])
def select():
    image_path = session.get("image_path")
    if not image_path:
        flash("Start by uploading a PDF.")
        return redirect(url_for("index"))

    if request.method == "POST":
        try:
            row_left = float(request.form["row_left"])
            row_top = float(request.form["row_top"])
            row_right = float(request.form["row_right"])
            row_bottom = float(request.form["row_bottom"])
            first_day_x = float(request.form["first_day_x"])
            last_day_x = float(request.form["last_day_x"])
        except Exception:
            flash("Please complete the row and date selection.")
            return redirect(url_for("select"))

        events = build_events_from_selection(
            Path(image_path),
            date.fromisoformat(session["month_start"]),
            (row_left, row_top, row_right, row_bottom),
            first_day_x,
            last_day_x,
            int(session["day_count"]),
            session.get("employee_name", ""),
        )
        session["events"] = events
        return redirect(url_for("review"))

    return render_template(
        "select.html",
        image_url=url_for("uploaded_image"),
        employee_name=session.get("employee_name", ""),
        month_start=session.get("month_start"),
        day_count=session.get("day_count"),
    )


@app.route("/uploaded-image")
def uploaded_image():
    image_path = session.get("image_path")
    if not image_path:
        return "", 404
    return send_file(image_path, mimetype="image/png")


@app.route("/review", methods=["GET", "POST"])
def review():
    events = session.get("events")
    if not events:
        flash("Please select the row first.")
        return redirect(url_for("select"))

    if request.method == "POST":
        updated = []
        for idx, old in enumerate(events):
            label = request.form.get(f"label_{idx}", old["label"])
            title = request.form.get(f"title_{idx}", LABEL_TITLES[label]).strip() or LABEL_TITLES[label]
            updated.append({
                "date": old["date"],
                "label": label,
                "rgb": old["rgb"],
                "title": title,
                "employee_name": old.get("employee_name", "Roster"),
                "coverage": old.get("coverage", 0.0),
                "reason": old.get("reason", ""),
            })

        session["events"] = updated
        action = request.form.get("action")
        if action == "download":
            return redirect(url_for("download_zip"))
        if action == "sync":
            return redirect(url_for("sync"))

        flash("Saved review changes.")
        return redirect(url_for("review"))

    return render_template(
        "review.html",
        events=events,
        calendar_names=session.get("calendar_names", DEFAULT_CALENDAR_NAMES),
    )


@app.route("/download-zip")
def download_zip():
    events = session.get("events")
    if not events:
        flash("Nothing to export yet.")
        return redirect(url_for("index"))

    memory = io.BytesIO()
    import zipfile

    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mediapulse_all.ics", make_ical(events))
        for label, filename in {
            "work": "mediapulse_work.ics",
            "ot": "mediapulse_overtime.ics",
            "unavailable": "mediapulse_unavailable_leave.ics",
            "off": "mediapulse_off.ics",
        }.items():
            zf.writestr(filename, make_ical(events, label=label))

    memory.seek(0)
    return send_file(
        memory,
        as_attachment=True,
        download_name="mediapulse_ical_exports.zip",
        mimetype="application/zip"
    )


@app.route("/sync", methods=["GET", "POST"])
def sync():
    events = session.get("events")
    if not events:
        flash("Nothing to sync yet.")
        return redirect(url_for("index"))

    if request.method == "POST":
        url = request.form.get("caldav_url", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        replace_existing = request.form.get("replace_existing") == "on"

        try:
            results = sync_to_icloud(
                events,
                url,
                username,
                password,
                session.get("calendar_names", DEFAULT_CALENDAR_NAMES),
                replace_existing=replace_existing,
            )
            flash("Sync completed: " + ", ".join(f"{count} events to {name}" for _, count, name in results))
            return redirect(url_for("review"))
        except Exception as exc:
            flash(f"Sync failed: {exc}")

    return render_template(
        "sync.html",
        calendar_names=session.get("calendar_names", DEFAULT_CALENDAR_NAMES),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)