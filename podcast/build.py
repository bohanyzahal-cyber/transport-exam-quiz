# -*- coding: utf-8 -*-
"""
בונה את פרקי הפודקאסט מקבצי התסריט שב-scripts/.

  cd podcast && python build.py            # בונה את כל הפרקים שהשתנו
  python build.py 03                       # בונה רק פרק מסוים
  python build.py --force                  # מתעלם מהמטמון ובונה הכל מחדש

פורמט התסריט (scripts/NN-slug.txt):
    # title: שם הפרק
    א: שורה שאומר המרצה (קול גברי)
    ש: שורה שאומרת המראיינת (קול נשי)
    ~3                     -> שקט של 3 שניות (זמן לחשוב על שאלה)
    שורה ריקה              -> הפסקה קצרה (0.45 שנייה)
    # הערה                 -> לא נקרא

כל שורה מסונתזת בנפרד ונשמרת במטמון לפי גיבוב הטקסט+הקול, כך ששינוי
שורה אחת אינו מחייב סינתזה מחדש של הפרק כולו.
"""
import asyncio
import hashlib
import os
import re
import subprocess
import sys
import time

import edge_tts

# קונסולת Windows ברירת מחדל היא cp1252 ומתה על עברית
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")
CACHE = os.path.join(HERE, ".cache")
OUT = os.path.join(HERE, "mp3")

VOICES = {"א": "he-IL-AvriNeural", "ש": "he-IL-HilaNeural"}
RATE = "+0%"
ALBUM = "ניהול מערכות תובלה ושינוע — פודקאסט תרגול"
GAP = 0.45                      # הפסקה בין דוברים
PARA_GAP = 0.9                  # הפסקה בין פסקאות
CONCURRENCY = 6                 # בקשות סינתזה במקביל

# ffmpeg מקודד הכל מחדש לפרמטר אחיד, כך שגם קטעי השקט מתחברים חלק
AUDIO_ARGS = ["-c:a", "libmp3lame", "-b:a", "64k", "-ar", "24000", "-ac", "1"]


def sh(args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode:
        sys.exit("ffmpeg נכשל:\n" + r.stderr.decode("utf-8", "replace")[-2000:])
    return r


def parse(path):
    """מפרק קובץ תסריט לרשימת קטעים: ('say', voice, text) או ('gap', seconds)."""
    title = os.path.splitext(os.path.basename(path))[0]
    parts, pending_gap = [], 0.0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("# title:"):
                title = line.split(":", 1)[1].strip()
                continue
            if line.startswith("#"):
                continue
            if not line:
                pending_gap = max(pending_gap, PARA_GAP)
                continue
            m = re.match(r"^~([\d.]+)$", line)
            if m:
                pending_gap = max(pending_gap, float(m.group(1)))
                continue
            m = re.match(r"^([אש])\s*:\s*(.+)$", line)
            if not m:
                sys.exit(f"שורה לא מזוהה ב-{os.path.basename(path)}:\n  {line}")
            if parts:
                parts.append(("gap", pending_gap or GAP))
            pending_gap = 0.0
            parts.append(("say", VOICES[m.group(1)], m.group(2)))
    return title, parts


def cache_path(kind, key):
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE, f"{kind}_{h}.mp3")


async def synth_one(voice, text, dest, sem):
    async with sem:
        for attempt in range(4):
            try:
                comm = edge_tts.Communicate(text, voice, rate=RATE)
                await comm.save(dest + ".part")
                break
            except Exception as e:               # השירות מגביל קצב מדי פעם
                if attempt == 3:
                    raise RuntimeError(f"סינתזה נכשלה: {text[:60]}") from e
                await asyncio.sleep(1.5 * (attempt + 1))
    os.replace(dest + ".part", dest)


async def synth_missing(jobs):
    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*(synth_one(v, t, d, sem) for v, t, d in jobs))


def silence(seconds):
    dest = cache_path("sil", f"{seconds:.3f}")
    if not os.path.exists(dest):
        sh(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono", "-t", f"{seconds:.3f}",
            *AUDIO_ARGS, dest])
    return dest


def build(path, force=False):
    title, parts = parse(path)
    num = re.match(r"^(\d+)", os.path.basename(path))
    num = num.group(1) if num else "0"
    out = os.path.join(OUT, f"{num} - {title}.mp3")

    jobs, pieces = [], []
    for p in parts:
        if p[0] == "gap":
            pieces.append(silence(p[1]))
            continue
        _, voice, text = p
        dest = cache_path("say", voice + "|" + RATE + "|" + text)
        if force or not os.path.exists(dest):
            jobs.append((voice, text, dest))
        pieces.append(dest)

    if not jobs and os.path.exists(out) and not force:
        print(f"  ✓ {os.path.basename(out)} — ללא שינוי")
        return out, None

    if jobs:
        print(f"  מסנתז {len(jobs)} שורות…")
        asyncio.run(synth_missing(jobs))

    listfile = os.path.join(CACHE, f"list_{num}.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write("file '" + p.replace("\\", "/").replace("'", r"'\''") + "'\n")

    sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", listfile, *AUDIO_ARGS,
        "-metadata", f"title={num} · {title}",
        "-metadata", f"album={ALBUM}",
        "-metadata", "artist=ניהול מערכות תובלה ושינוע",
        "-metadata", f"track={int(num)}",
        "-metadata", "genre=Education",
        "-id3v2_version", "3",
        out])
    return out, len(jobs)


def duration(path):
    # OneDrive מסנכרן את הקובץ בדיוק כשהוא נכתב, וההרצה הראשונה נכשלת לפעמים
    for _ in range(3):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True)
        try:
            return float(r.stdout.decode().strip())
        except ValueError:
            time.sleep(0.5)
    return 0.0


INDEX_HEAD = """<!DOCTYPE html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>פודקאסט — ניהול מערכות תובלה ושינוע</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#16202b;--dim:#5b6b7d;--line:#dde4ec;--accent:#1a5fb4}
@media(prefers-color-scheme:dark){:root{--bg:#12171d;--card:#1b222b;--ink:#e8edf3;--dim:#93a3b5;--line:#2b3542;--accent:#6ea8ff}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 64px;background:var(--bg);color:var(--ink);
     font:16px/1.6 "Segoe UI",system-ui,sans-serif;direction:rtl}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--dim);margin:0 0 6px}
.tot{color:var(--dim);font-size:.85rem;margin:0 0 24px}
.ep{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin-bottom:12px}
.hd{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
.num{background:var(--accent);color:#fff;border-radius:6px;padding:1px 8px;
     font-size:.8rem;font-weight:700;flex:none}
.ttl{font-weight:600;flex:1}
.dur{color:var(--dim);font-size:.85rem;flex:none;font-variant-numeric:tabular-nums}
audio{width:100%;height:36px}
.nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 20px}
.nav a{flex:1 1 240px;display:inline-flex;align-items:center;gap:8px;
  background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:9px 14px;color:var(--ink);text-decoration:none;font-size:.85rem;transition:.15s}
.nav a:hover{border-color:var(--accent)}
.nav b{font-weight:650}
.nav em{font-style:normal;color:var(--dim)}
.note{color:var(--dim);font-size:.85rem;margin-top:28px;border-top:1px solid var(--line);padding-top:16px}
</style>
<div class="wrap">
<h1>פודקאסט — ניהול מערכות תובלה ושינוע</h1>
<p class="sub">פרק לכל נושא. בסוף כל פרק — שאלות לדרך עם השהייה לחשוב.</p>
<div class="nav">
<a href="../index.html">🎯 <span><b>בוחן התרגול</b> <em>· שאלות אמריקאיות וסימולציית מבחן</em></span></a>
<a href="../exam.html">📄 <span><b>חוברת החומר הפתוח</b> <em>· להדפסה לפני המבחן</em></span></a>
</div>
"""


def write_index(rows):
    total = sum(d for _, d, _ in rows)
    parts = [INDEX_HEAD,
             f'<p class="tot">{len(rows)} פרקים · '
             f'{int(total // 3600)} שעות ו-{int(total % 3600 // 60)} דקות</p>\n']
    for name, d, _ in rows:
        num, title = name[:-4].split(" - ", 1)
        href = "mp3/" + name.replace("#", "%23").replace("?", "%3F")
        parts.append(
            f'<div class="ep"><div class="hd">'
            f'<span class="num">{num}</span>'
            f'<span class="ttl">{title}</span>'
            f'<span class="dur">{int(d // 60)}:{int(d % 60):02d}</span></div>'
            f'<audio controls preload="none" src="{href}"></audio></div>\n')
    parts.append('<p class="note">הקבצים עצמם נמצאים בתיקיית <code>mp3</code> — '
                 'אפשר להעתיק אותם לטלפון ולהאזין בלי אינטרנט. '
                 'הם מתויגים כאלבום אחד לפי מספר פרק, כך שכל נגן ישמור על הסדר.</p>\n')
    parts.append("</div>\n")
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write("".join(parts))


def main():
    args = [a for a in sys.argv[1:]]
    force = "--force" in args
    only = [a for a in args if not a.startswith("--")]

    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    files = sorted(f for f in os.listdir(SCRIPTS) if f.endswith(".txt"))
    if only:
        files = [f for f in files if any(f.startswith(o) for o in only)]
    if not files:
        sys.exit("לא נמצאו תסריטים ב-scripts/")

    total = 0.0
    rows = []
    for f in files:
        print(f"\n▸ {f}")
        out, _ = build(os.path.join(SCRIPTS, f), force)
        d = duration(out)
        total += d
        size = os.path.getsize(out) / 1e6
        rows.append((os.path.basename(out), d, size))
        print(f"  → {int(d // 60)}:{int(d % 60):02d}  ({size:.1f} MB)")

    if not only:
        write_index(rows)          # רק בבנייה מלאה, אחרת המפתח ייחתך

    print("\n" + "─" * 58)
    for name, d, size in rows:
        print(f"{int(d // 60):>3}:{int(d % 60):02d}  {size:>5.1f} MB  {name}")
    print("─" * 58)
    print(f"סה״כ {len(rows)} פרקים · {int(total // 3600)} שעות "
          f"{int(total % 3600 // 60)} דקות · "
          f"{sum(r[2] for r in rows):.0f} MB")


if __name__ == "__main__":
    main()
