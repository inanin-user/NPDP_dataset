#!/usr/bin/env python3
"""file_filter.py - decide which files are backups/drafts/dated copies and must NOT be scanned.

Default rules (evaluated on the file name, case-insensitive):
  date_suffix       name (without extension) ends with a date, e.g. prod_get-20220317.php,
                    prod_get_2022-03-17.php, prod_get_17032022.php (optionally followed by a time)
  double_underscore name contains '__'
  whitespace        name contains a space/tab, e.g. 'prod_mas - copy (2).php'
  substrings        name contains any of ["bak"]
  extra_regex       your own regular expressions (matched against the full file name)
  allow             names that are NEVER excluded (exact names or regex)

Use as a library:
    from file_filter import FileFilter
    flt = FileFilter(config_path="filter_rules.json")   # config optional
    flt.reasons("prod_get-20220317.php")                # -> ['date_suffix']
    flt.is_excluded("prod_get.php")                     # -> False

Use as a tool:
    python file_filter.py --self-test
    python file_filter.py --test prod_get.php "prod_draft_mas - __ (2).php"
    python file_filter.py --src D:\\AppServ\\htdocs\\Test --csv excluded.csv
    python file_filter.py --write-default-config filter_rules.json
"""
import argparse
import csv
import json
import os
import re
import sys

DEFAULT_CONFIG = {
    "date_suffix": True,
    "short_date_yymmdd": False,      # 6-digit dates like 220317; off because it can hit real names
    "double_underscore": True,
    "whitespace": True,
    "substrings": ["bak"],
    "extra_regex": [],
    "allow": [],
}

SEP = r"[-_.]?"
YEAR = r"(?:19|20)\d{2}"
MON = r"(?:0[1-9]|1[0-2])"
DAY = r"(?:0[1-9]|[12]\d|3[01])"
TIME = r"(?:[-_ ]?\d{4,6})?"
DATE_PATTERNS = [
    re.compile(YEAR + SEP + MON + SEP + DAY + TIME + r"$"),      # 20220317, 2022-03-17, 20220317_1530
    re.compile(DAY + SEP + MON + SEP + YEAR + TIME + r"$"),      # 17032022, 17-03-2022
]
SHORT_DATE = re.compile(r"\d{2}" + MON + DAY + r"$")             # 220317


class FileFilter:
    def __init__(self, config=None, config_path=None):
        cfg = dict(DEFAULT_CONFIG)
        if config_path:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        if config:
            cfg.update(config)
        self.cfg = cfg
        self.extra = [re.compile(p, re.I) for p in cfg["extra_regex"]]
        self.allow = [re.compile(p, re.I) for p in cfg["allow"]]
        self.subs = [s.lower() for s in cfg["substrings"]]

    def reasons(self, filename):
        """Return a list of reasons the file is excluded (empty list = scan it)."""
        name = os.path.basename(filename)
        if any(rx.fullmatch(name) for rx in self.allow):
            return []
        stem = os.path.splitext(name)[0].lower()
        low = name.lower()
        out = []
        if self.cfg["date_suffix"]:
            if any(p.search(stem) for p in DATE_PATTERNS) or \
               (self.cfg["short_date_yymmdd"] and SHORT_DATE.search(stem)):
                out.append("date_suffix")
        if self.cfg["double_underscore"] and "__" in name:
            out.append("double_underscore")
        if self.cfg["whitespace"] and re.search(r"\s", name):
            out.append("whitespace")
        for s in self.subs:
            if s and s in low:
                out.append("substring:" + s)
        for rx in self.extra:
            if rx.search(name):
                out.append("regex:" + rx.pattern)
        return out

    def is_excluded(self, filename):
        return bool(self.reasons(filename))


def self_test():
    flt = FileFilter()
    cases = [
        ("prod_draft_get-20220317.php", True), ("prod_get_2022-03-17.php", True),
        ("prod_get_17032022.php", True), ("prod_get_20220317_1530.php", True),
        ("prod_draft_mas - __ (2).php", True), ("prod_mas copy.php", True),
        ("prod_mas__old.php", True), ("prod_mas_bak.php", True), ("PROD_BAK2.PHP", True),
        ("prod_get.php", False), ("prod_main.php", False), ("report_v2.php", False),
        ("prod_get_20221340.php", False),      # month 13 is not a date
        ("invoice_00123456.php", False),       # not a date
    ]
    bad = 0
    for name, expected in cases:
        got = flt.is_excluded(name)
        ok = got == expected
        bad += not ok
        print(("PASS" if ok else "FAIL"), f"{name!r:40} excluded={got} {flt.reasons(name)}")
    print("self-test:", "OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON rules file")
    ap.add_argument("--src", help="directory to scan and report")
    ap.add_argument("--ext", nargs="*", default=[".php", ".inc", ".phtml"])
    ap.add_argument("--skip", nargs="*", default=[".git", "node_modules", "vendor"])
    ap.add_argument("--csv", help="write excluded files (path, reason) to this CSV")
    ap.add_argument("--test", nargs="*", help="test file names")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--write-default-config", metavar="PATH")
    a = ap.parse_args()

    if a.write_default_config:
        with open(a.write_default_config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print("written", a.write_default_config)
        return 0
    if a.self_test:
        return self_test()
    flt = FileFilter(config_path=a.config)
    if a.test:
        for n in a.test:
            print(f"{n!r}: excluded={flt.is_excluded(n)} {flt.reasons(n)}")
    if a.src:
        kept, excl = 0, []
        for root, dirs, names in os.walk(a.src):
            dirs[:] = [d for d in dirs if d not in a.skip]
            for n in names:
                if not n.lower().endswith(tuple(a.ext)):
                    continue
                why = flt.reasons(n)
                if why:
                    excl.append((os.path.relpath(os.path.join(root, n), a.src).replace("\\", "/"), ";".join(why)))
                else:
                    kept += 1
        print(f"scan={kept} excluded={len(excl)}")
        for p, w in excl[:50]:
            print("  excluded:", p, "->", w)
        if a.csv:
            with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["file", "reason"])
                w.writerows(excl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
