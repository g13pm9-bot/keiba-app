"""保存済みの実OCR結果とJV-Link出馬表を照合する手動確認用スクリプト。

対象は20261003 東京11R。採点・OCR再実行・Streamlit接続は行わない。
照合はauto_correct_by_horse_number=Trueを指定し、安全判定結果も表示する。
実行方法:
  python test_reconciliation_real.py --ocr-json "保存済みOCR結果.json"
  または、下記OCR_DATAに実際のOCR辞書を設定して:
  python test_reconciliation_real.py

OCR_DATA未設定の場合、JV-Link取得前に停止する。
架空データやJRA-VAN値をOCR原本の代わりに使用しない。
importのみではCOM作成・通信を行わない。
"""

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import unicodedata


RACE_DATE = "20261003"
COURSE = "東京"
RACE_NUMBER = 11
FIELDS = ("horse_number", "horse_name", "frame_number", "jockey_name")
STATUSES = ("matched", "corrected", "needs_review", "external_only")

# 対象レースの実OCR結果が未提供のため、捏造せず未設定にしている。
# 実際の {"race": {...}, "horses": [{...}, ...]} をそのまま設定できる。
# 重複検出のため、重複行を削除する前のOCR結果を使用すること。
OCR_DATA = None


def load_ocr_data(json_path=None):
    if json_path is not None:
        with Path(json_path).open("r", encoding="utf-8-sig") as source:
            data = json.load(source)
        origin = str(Path(json_path).resolve())
    else:
        data = deepcopy(OCR_DATA)
        origin = "スクリプト内のOCR_DATA"
    if data is None:
        raise ValueError(
            "対象レースのOCR原本が未設定です。OCR_DATAに実際の辞書を入れるか、"
            "--ocr-jsonで保存済みJSONを指定してください。JV-Link取得は行っていません。"
        )
    if not isinstance(data, dict) or not isinstance(data.get("horses"), list):
        raise ValueError("OCR結果はhorses配列を持つ辞書で指定してください。")
    if not data["horses"]:
        raise ValueError("OCR結果のhorsesが空です。実OCR結果を指定してください。取得は行いません。")
    if any(not isinstance(horse, dict) for horse in data["horses"]):
        raise ValueError("OCR結果のhorsesの各要素は辞書で指定してください。")
    if data.get("race") is not None and not isinstance(data["race"], dict):
        raise ValueError("OCR結果のraceは辞書またはNoneで指定してください。")
    return data, origin


def displayed_number(row):
    values = row["fields"]["horse_number"]
    for key in ("final_value", "ocr_value", "external_value"):
        if values[key] is not None:
            return values[key]
    return None


def number_sort_key(row):
    number = displayed_number(row)
    if type(number) is int and number > 0:
        return (0, number)
    if isinstance(number, str):
        text = unicodedata.normalize("NFKC", number).strip()
        if text.isascii() and text.isdecimal() and int(text) > 0:
            return (0, int(text))
    return (1, 0)


def print_result(result):
    print("照合結果（馬単位の行。重複・未対応行も省略せず表示）")
    for row in sorted(result["reconciliations"], key=number_sort_key):
        fields = row["fields"]
        output = {
            "行ID": row["row_id"],
            "馬番": displayed_number(row),
            "OCR馬名": fields["horse_name"]["ocr_value"],
            "JRA-VAN馬名": fields["horse_name"]["external_value"],
            "final_value": {field: fields[field]["final_value"] for field in FIELDS},
            "status": row["status"],
            "確認理由": row["reasons"],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    print("集計（reconciliationsの行statusを集計。各項目のstatusは重複加算しません）")
    auto_correction = result["auto_correction"]
    for key in ("requested", "enabled", "reason"):
        print(f"auto_correction.{key}: {auto_correction[key]}")
    counts = Counter(row["status"] for row in result["reconciliations"])
    for status in STATUSES:
        print(f"{status}件数: {counts[status]}")
    print(f"ready_for_scoring: {result['ready_for_scoring']}")


def main():
    parser = argparse.ArgumentParser(description="20261003 東京11Rの実OCR結果とJV-Link出馬表を照合")
    parser.add_argument("--ocr-json", help="保存済みの実OCR結果JSON。省略時はOCR_DATAを使用")
    args = parser.parse_args()
    # テスト実行時も既存モジュールのpyc生成・更新を避ける。
    sys.dont_write_bytecode = True
    try:
        # OCR入力を検証してからJV-Link取得を行う。入力原本は編集しない。
        ocr_data, origin = load_ocr_data(args.ocr_json)
        from racecard_jvlink import fetch_racecard
        from reconciliation import reconcile_racecard

        print(f"対象: {RACE_DATE} {COURSE}{RACE_NUMBER}R")
        print(f"OCR原本: {origin}")
        external_data = fetch_racecard(RACE_DATE, COURSE, RACE_NUMBER)
        result = reconcile_racecard(
            ocr_data,
            external_data,
            auto_correct_by_horse_number=True,
        )
        print("race_check: " + json.dumps(result["race_check"], ensure_ascii=False))
        print_result(result)
        return 0
    except KeyboardInterrupt:
        print("確認を中止しました。", file=sys.stderr)
        return 130
    except Exception as exc:
        # JVLinkErrorの文字列にはメソッド名・理由・該当するコードが含まれる。
        print(f"確認失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
        details = getattr(exc, "details", None)
        if details:
            print("詳細: " + json.dumps(details, ensure_ascii=False, default=str), file=sys.stderr)
        cleanup_error = getattr(exc, "cleanup_error", None)
        if cleanup_error is not None:
            print(f"後処理失敗: {cleanup_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
