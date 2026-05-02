# MediaPulse PDF to iCal / iCloud

This Flask app lets you:

1. Upload a MediaPulse roster PDF
2. Click your row and the first/last day cells
3. Auto-classify each day by cell colour:
   - work = light green
   - OT = red
   - unavailable/leave = light blue
   - off = white/grey
4. Review and correct the extracted days
5. Download `.ics` files or sync directly to iCloud Calendar via CalDAV

## Important Apple Calendar limitation

Apple Calendar does **not** reliably support different colours for individual events in the same calendar.
The practical way to preserve your colour coding is:

- Work -> one calendar
- OT -> one calendar
- Unavailable/Leave -> one calendar
- Off -> one calendar

This app already follows that approach during iCloud sync.

## Install

```bash
python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# or
.venv\Scripts\activate    # Windows

pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5001
```

## How to use

### Step 1
Upload the PDF.

Enter:
- employee name
- first date shown in the row
- number of day cells

### Step 2
On the selection screen click:

1. top-left of your row
2. bottom-right of your row
3. centre of the first day cell
4. centre of the last day cell

### Step 3
Review the extracted days and fix any wrong classifications manually.

### Step 4
Either:
- download the ICS zip, or
- sync to iCloud

## iCloud / Apple setup

For direct sync use CalDAV.

Typical server URL:

```text
https://caldav.icloud.com/
```

Use:
- your Apple ID email as username
- an **app-specific password**

Apple Calendar colours are calendar-level. After the first sync, set each calendar colour once in Apple Calendar:

- MediaPulse Work -> green
- MediaPulse Overtime -> red
- MediaPulse Unavailable/Leave -> blue
- MediaPulse Off -> grey

## Notes

- This is designed for repeated use with similar monthly MediaPulse roster PDFs.
- The PDF export is image-based, so the app uses the row colour blocks rather than text extraction.
- If your export colours drift, tune the colour fields on the first screen.
- For safety, the sync only removes events it previously created when "Replace previously auto-imported events" is ticked.

## Security

Do not hardcode your Apple password.
Prefer app-specific passwords for iCloud.
