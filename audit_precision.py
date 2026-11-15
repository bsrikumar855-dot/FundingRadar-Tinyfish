#!/usr/bin/env python3
"""Read a hand-filled audit.csv (column 'correct y/n') and write the measured precision into SUBMISSION.md.

  python audit_precision.py demos/agritech-india/audit.csv --label "demo 1 (agritech)"

precision = correct / (correct + wrong). Blank rows are reported but not counted. The result goes between
<!-- audit:start --> and <!-- audit:end --> in SUBMISSION.md, one line per label (re-running a label replaces its line)."""
import argparse, csv, re, sys
from datetime import date

START, END = "<!-- audit:start -->", "<!-- audit:end -->"


def score(path):
    c = w = b = 0
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = (row.get("correct y/n") or "").strip().lower()
            if v in ("y", "yes", "1", "true", "correct"):
                c += 1
            elif v in ("n", "no", "0", "false", "wrong"):
                w += 1
            else:
                b += 1
    return c, w, b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv")
    ap.add_argument("--submission", default="SUBMISSION.md")
    ap.add_argument("--label", default="audit")
    a = ap.parse_args(argv)
    c, w, b = score(a.csv)
    prec = c / (c + w) if c + w else None
    line = (f"- {a.label}: {c} / {c + w} correct = {prec * 100:.1f}% precision" if prec is not None else f"- {a.label}: no rows marked") + \
           f" ({b} blank; checked {date.today().isoformat()}; file `{a.csv}`)"
    with open(a.submission, encoding="utf-8") as f:
        txt = f.read()
    if START not in txt:
        txt = txt.rstrip() + f"\n\n## Hand-checked precision (audit sample, seed 42)\n\n{START}\n{END}\n"
    head, rest = txt.split(START, 1)
    body, tail = rest.split(END, 1)
    lines = [l for l in body.strip().splitlines() if l.strip() and not l.startswith(f"- {a.label}:")]
    lines.append(line)
    txt = head + START + "\n" + "\n".join(lines) + "\n" + END + tail
    with open(a.submission, "w", encoding="utf-8") as f:
        f.write(txt)
    print(line)
    return {"correct": c, "wrong": w, "blank": b, "precision": prec}


if __name__ == "__main__":
    main()
    sys.exit(0)
