"""Copy license texts from a license-checker report for the Mermaid bundle."""
import json
import re
import shutil
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 generate_mermaid_licenses.py license-checker-report.json")
    report_path = Path(sys.argv[1]).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    output = Path(__file__).resolve().parent / "web/vendor/mermaid"
    licenses = output / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for old_file in licenses.glob("*.txt"):
        old_file.unlink()

    rows = []
    for package, metadata in sorted(report.items(), key=lambda item: item[0].casefold()):
        if package == "mermaid@10.9.3":
            continue
        license_path = Path(metadata.get("licenseFile", ""))
        if not license_path.is_file():
            raise SystemExit(f"missing license file for {package}: {license_path}")
        safe_name = re.sub(r"[^A-Za-z0-9@.+_-]+", "_", package)
        filename = f"{safe_name}.txt"
        shutil.copyfile(license_path, licenses / filename)
        repository = metadata.get("repository", "")
        rows.append(f"| `{package}` | {metadata.get('licenses', 'Unknown')} | [`licenses/{filename}`](licenses/{filename}) | {repository} |")

    notice = """# Mermaid bundle dependency licenses

This inventory was generated from the production dependency tree in Mermaid
10.9.3's upstream `pnpm-lock.yaml`. License texts are copied from the installed
packages identified by that lockfile. Mermaid's own MIT license is in
[`LICENSE`](LICENSE).

| Package | Declared license | License text | Upstream repository |
| --- | --- | --- | --- |
""" + "\n".join(rows) + "\n"
    (output / "THIRD_PARTY_NOTICES.md").write_text(notice, encoding="utf-8")


if __name__ == "__main__":
    main()
