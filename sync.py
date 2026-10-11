#!/usr/bin/env python3
"""sync.py: refreshes what minictl carries from elsewhere, so it runs without a firmware checkout.

    python sync.py --fork ~/minichord --stock upstream/main --minicontrol ~/minicontrol

From the minichord fork's checkout:
  minictl/describer/   interpret.py, preset_maker.py and songs.json from firmware/preset_maker, as
                       they are (the describer is written there; edit it there, then sync)
  minictl/data/fork/   parameters.json, shared_presets.json and parameter_lookup.h (which settings
                       a knob sweeps whole), and the firmware version from src/main.cpp
From Ben's stock firmware, a ref in the same checkout (git fetch upstream first):
  minictl/data/stock/  parameters.json, shared_presets.json and the version
From minicontrol:
  minictl/data/options.json, the names its dropdowns give each setting's values

Then run the tests (tests/test_minictl.py, tests/parity_test.py) and commit what changed.
"""
import argparse, json, os, re, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "minictl")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def version_of(main_cpp):
    m = re.search(r"int\s+version_ID\s*=\s*(\d+)", main_cpp)
    if not m:
        raise SystemExit("no version_ID in main.cpp")
    return int(m.group(1))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser(description="Refresh minictl's describer and firmware data")
    ap.add_argument("--fork", default="~/minichord", help="the minichord fork's checkout")
    ap.add_argument("--stock", default="upstream/main", help="the stock firmware's ref in that checkout")
    ap.add_argument("--minicontrol", default="~/minicontrol", help="minicontrol's folder")
    args = ap.parse_args()
    fork = os.path.expanduser(args.fork)
    fw = os.path.join(fork, "firmware")

    for name in ("interpret.py", "preset_maker.py", "songs.json"):
        shutil.copy(os.path.join(fw, "preset_maker", name), os.path.join(PKG, "describer", name))
    shutil.copy(os.path.join(fw, "generator", "parameters.json"), os.path.join(PKG, "data", "fork", "parameters.json"))
    shutil.copy(os.path.join(fw, "minicontrol", "json", "shared_presets.json"),
                os.path.join(PKG, "data", "fork", "shared_presets.json"))
    shutil.copy(os.path.join(fw, "lib", "potentiometer", "src", "parameter_lookup.h"),
                os.path.join(PKG, "data", "fork", "parameter_lookup.h"))
    with open(os.path.join(fw, "src", "main.cpp")) as f:
        fork_version = version_of(f.read())

    write(os.path.join(PKG, "data", "stock", "parameters.json"),
          git(fork, "show", f"{args.stock}:firmware/generator/parameters.json"))
    write(os.path.join(PKG, "data", "stock", "shared_presets.json"),
          git(fork, "show", f"{args.stock}:firmware/minicontrol/json/shared_presets.json"))
    stock_version = version_of(git(fork, "show", f"{args.stock}:firmware/src/main.cpp"))

    with open(os.path.join(os.path.expanduser(args.minicontrol), "parameters.json")) as f:
        d = json.load(f)
    options = {}
    for section in d.values():
        for p in section if isinstance(section, list) else []:
            if p.get("options"):
                options[str(p["sysex_adress"])] = {str(o["value"]): o["label"] for o in p["options"]}
    with open(os.path.join(PKG, "data", "options.json"), "w") as f:
        json.dump(dict(sorted(options.items(), key=lambda kv: int(kv[0]))), f, indent=1, ensure_ascii=False)
        f.write("\n")

    info = {"fork": {"version": fork_version, "commit": git(fork, "rev-parse", "--short", "HEAD").strip()},
            "stock": {"version": stock_version, "commit": git(fork, "rev-parse", "--short", args.stock).strip()}}
    with open(os.path.join(PKG, "data", "firmware.json"), "w") as f:
        json.dump(info, f, indent=1)
        f.write("\n")
    print(f"fork firmware {fork_version} ({info['fork']['commit']}), stock firmware {stock_version} "
          f"({info['stock']['commit']}), {len(options)} settings with named values")


if __name__ == "__main__":
    main()
