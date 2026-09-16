"""DEV ONLY. Harvest Kokoro phonemes for a track's corner and section names through
espeak-ng (GPL) so the SHIPPED table carries only data. Run from a throwaway venv:
    python -m venv .venv-harvest && .venv-harvest/Scripts/pip install kokoro-onnx==0.6.1 phonemizer espeakng-loader
    .venv-harvest/Scripts/python scripts/harvest_pronunciations.py circuit_de_spa_francorchamps [--write]
Prints the block; --write merges it into simcoach/tracks/<slug>.json (keys that already
exist are kept unless --force). Only names whose phonemes are entirely inside Kokoro's
vocabulary are written. A name that is a single common English word (it would rewrite
ordinary prose -- "Source" is bad, "La Source" is fine) is never harvested automatically;
pass it via --exclude and it is listed under REJECTED instead.
"""
import argparse
import json
import pathlib
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--exclude", default="",
                     help="comma-separated names to drop from the table (e.g. single common English words)")
    a = ap.parse_args()
    exclude = {n.strip() for n in a.exclude.split(",") if n.strip()}

    path = pathlib.Path("simcoach/tracks") / f"{a.slug}.json"
    raw_bytes = path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))
    names = sorted({c["name"] for c in data.get("corners", []) if c.get("name")}
                    | {s["name"] for s in data.get("sections", []) if s.get("name")})

    from kokoro_onnx.config import DEFAULT_VOCAB
    from kokoro_onnx.tokenizer import Tokenizer  # real GPL path, dev machine only
    tok = Tokenizer()
    table, rejected = {}, []
    for n in names:
        if n in exclude:
            rejected.append((n, "excluded: single common English word"))
            continue
        ph = tok.phonemize(n, lang="en-us").strip()
        if ph and all(c in DEFAULT_VOCAB or c == " " for c in ph):
            table[n] = ph
        else:
            rejected.append((n, ph or "(empty)"))

    print(json.dumps(table, ensure_ascii=False, indent=2))
    for n, ph in rejected:
        print(f"REJECTED {n!r}: {ph!r}", file=sys.stderr)

    if a.write:
        existing = data.get("pronunciations") or {}
        merged = {**table, **existing} if not a.force else {**existing, **table}
        data["pronunciations"] = dict(sorted(merged.items()))
        # Preserve the file's existing indent (2), line endings, and trailing-newline
        # state; only the new "pronunciations" key is added, at the end of the object.
        out = json.dumps(data, ensure_ascii=False, indent=2)
        if raw_bytes.endswith(b"\n"):
            out += "\n"
        if b"\r\n" in raw_bytes:
            out = out.replace("\n", "\r\n")
        path.write_bytes(out.encode("utf-8"))
        print(f"wrote {len(merged)} entries to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
