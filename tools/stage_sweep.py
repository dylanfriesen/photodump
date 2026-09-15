"""Queue a sweep spec so it renders the next time the desktop is awake.

  python3 tools/stage_sweep.py tools/sweeps/upscale-hair-lettering.json
  python3 tools/stage_sweep.py SPEC --url http://127.0.0.1:8097   # test instance
  python3 tools/stage_sweep.py SPEC --hold "2026-09-16 10:00"      # UTC

Stdlib only, so it runs on the host. Results appear at /sweeps.html once the
renders land; the whole grid arrives as one email.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--url", default="http://127.0.0.1:8096")
    ap.add_argument("--hold", help="UTC 'YYYY-MM-DD HH:MM'; the grid waits until then")
    args = ap.parse_args()

    spec = json.load(open(args.spec))
    spec = {k: v for k, v in spec.items() if not k.startswith("_")}
    if args.hold:
        spec["not_before"] = args.hold
    req = urllib.request.Request(
        f"{args.url}/api/sweeps", data=json.dumps(spec).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        body = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        print(f"refused ({e.code}): {e.read().decode()}", file=sys.stderr)
        return 1
    n = len(body["queued"])
    print(f"queued sweep {body['name']!r}: {body['cells']} cells x {len(body['seeds'])} seeds "
          f"= {n} first-pass jobs (jobs {body['queued'][0]}-{body['queued'][-1]})")
    if spec["base"].get("second_pass"):
        print(f"each chains a pass 2 when it lands: {2 * n} renders in total")
    print(f"results: {args.url}/sweeps.html#{body['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
