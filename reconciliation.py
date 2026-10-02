"""OCR原本とJV-Link出馬表から、対応が確認できた馬の確定値を作る純粋な照合。

reconcile_racecard(ocr_data, external_data, auto_correct_by_horse_number=False)
入力はどちらも {'race': {...}, 'horses': [dict, ...]}。
入力は変更せず、COM・通信・OCR・採点処理は行わない。
horsesには4項目の確定値がそろったOCR行だけを収録する。
調教・近走・コメント等はそのOCR行から深いコピーでそのまま保持する。
名前の類似度による自動対応付けや、馬名だけによる馬番変更は行わない。
明示的な自動補正モードでは、全頭の馬番とレース情報の安全条件を確認してから
馬番対応で外部値を採用する。原本と補正理由は保持する。
"""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import unicodedata


FIELDS = ("horse_number", "horse_name", "frame_number", "jockey_name")
COURSES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}


def _text(value):
    if not isinstance(value, str):
        return ""
    return "".join(unicodedata.normalize("NFKC", value).split())


def _name(value):
    text = _text(value)
    return "" if text.casefold() in {"不明", "unknown", "?", "-"} else text


def _number(value, maximum):
    if type(value) is int:
        return value if 1 <= value <= maximum else None
    if isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).strip()
        if text.isascii() and text.isdecimal() and 1 <= int(text) <= maximum:
            return int(text)
    return None


def _usable(field, value):
    if field in ("horse_number", "frame_number"):
        return _number(value, 28 if field == "horse_number" else 8)
    return value if _name(value) else None


def _validate_input(data, label):
    if not isinstance(data, dict) or not isinstance(data.get("horses"), list):
        raise ValueError(f"{label}はhorses配列を持つ辞書で指定してください。")
    if any(not isinstance(horse, dict) for horse in data["horses"]):
        raise ValueError(f"{label}.horsesの各要素は辞書で指定してください。")
    if data.get("race") is not None and not isinstance(data["race"], dict):
        raise ValueError(f"{label}.raceは辞書またはNoneで指定してください。")


def _race_value(field, value):
    if field == "race_date":
        text = _text(value)
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                # 年月日の省略は推定しない。
                if len(text) in (8, 10):
                    return parsed.strftime("%Y%m%d")
            except ValueError:
                pass
        return None
    if field == "race_number":
        return _number(value, 12)
    text = str(value) if type(value) is int else _text(value)
    return COURSES.get(text.zfill(2), text) if text else None


def _race_check(ocr_data, external_data):
    ocr = ocr_data.get("race") or {}
    external = external_data.get("race") or {}
    checked = []
    conflicts = []
    for field in ("race_date", "course", "race_number"):
        # 現在のOCRはrace.date、JV-Linkはrace.race_date。
        ocr_value = ocr.get(field) if field != "race_date" else ocr.get("race_date") or ocr.get("date")
        ext_value = external.get(field) if field != "race_date" else external.get("race_date") or external.get("date")
        if ocr_value in (None, "") or ext_value in (None, ""):
            continue
        left, right = _race_value(field, ocr_value), _race_value(field, ext_value)
        checked.append(field)
        if left is None or right is None or left != right:
            conflicts.append({"field": field, "ocr_value": deepcopy(ocr_value),
                              "external_value": deepcopy(ext_value)})
    return {"status": "needs_review" if conflicts else "matched" if len(checked) == 3 else "not_fully_checked",
            "checked_fields": checked, "conflicts": conflicts}


def _entry(ocr, external, *, confirmed, auto_correct=False):
    fields = {}
    for field in FIELDS:
        left = ocr.get(field) if ocr is not None else None
        right = external.get(field) if external is not None else None
        final = None
        status = "needs_review"
        if confirmed:
            ext_usable = _usable(field, right)
            ocr_usable = _usable(field, left)
            if ext_usable is not None:
                final = ext_usable
                if auto_correct:
                    status = "matched" if type(left) is type(final) and left == final else "corrected"
                elif ocr_usable is None:
                    status = "external_only"
                else:
                    status = "matched" if type(left) is type(final) and left == final else "corrected"
            elif not auto_correct and right in (None, "") and ocr_usable is not None:
                # 外部値が未設定なら、対応が確定したOCR値を保持する。
                final = ocr_usable
                status = "ocr_only"
        elif ocr is None:
            status = "external_only"
        fields[field] = {"ocr_value": deepcopy(left), "external_value": deepcopy(right),
                         "final_value": deepcopy(final), "status": status}
    return fields


def _reason(code, message, fields=FIELDS):
    return {"code": code, "message": message, "fields": list(fields)}


def _auto_correction_check(requested, ocr_horses, ext_horses, race_check):
    if not requested:
        return {"requested": False, "enabled": False, "reason": "not_requested", "failures": []}
    failures = []
    ocr_numbers = [horse.get("horse_number") for horse in ocr_horses]
    ext_numbers = [_number(horse.get("horse_number"), 28) for horse in ext_horses]
    integers = all(type(number) is int for number in ocr_numbers)
    if not integers:
        failures.append("ocr_horse_numbers_not_all_integers")
    if any(_number(number, 28) is None for number in ocr_numbers):
        failures.append("ocr_horse_number_missing_or_invalid")
    if integers and len(set(ocr_numbers)) != len(ocr_numbers):
        failures.append("duplicate_ocr_horse_number")
    if any(number is None for number in ext_numbers):
        failures.append("external_horse_number_missing_or_invalid")
    valid_external = [number for number in ext_numbers if number is not None]
    if len(set(valid_external)) != len(valid_external):
        failures.append("duplicate_external_horse_number")
    if integers and any(number not in valid_external for number in ocr_numbers):
        failures.append("ocr_horse_number_not_in_external")
    if len(ocr_horses) != len(ext_horses):
        failures.append("horse_count_mismatch")
    if not ocr_horses or not ext_horses:
        failures.append("empty_racecard")
    if race_check["status"] != "matched":
        failures.append("race_mismatch" if race_check["conflicts"] else "race_information_incomplete")
    return {"requested": True, "enabled": not failures,
            "reason": failures[0] if failures else "all_horse_numbers_verified", "failures": failures}


def reconcile_racecard(ocr_data, external_data, auto_correct_by_horse_number=False):
    """原本・フィールド別照合・確認待ち・採点用horsesを返す。

    正規化後の馬番と馬名の両方が一意に一致した場合だけ対応を確定する。
    status: matched / corrected / needs_review / ocr_only / external_only。
    ready_for_scoring=Falseの結果を、全馬の確定結果として採点に渡さないこと。
    元の入力で重複が削除済みなら、その削除前の重複は検出できない。
    自動補正は明示的にTrueを指定し、全頭の整数馬番・一意性・存在・頭数と
    レース3項目の一致が検証できた場合だけ有効。不成立なら従来の照合に戻る。
    """
    _validate_input(ocr_data, "ocr_data")
    _validate_input(external_data, "external_data")
    if type(auto_correct_by_horse_number) is not bool:
        raise ValueError("auto_correct_by_horse_numberはTrueまたはFalseで指定してください。")
    ocr = deepcopy(ocr_data)
    external = deepcopy(external_data)
    ocr_horses, ext_horses = ocr["horses"], external["horses"]
    race_check = _race_check(ocr, external)
    auto_correction = _auto_correction_check(
        auto_correct_by_horse_number, ocr_horses, ext_horses, race_check)
    auto_enabled = auto_correction["enabled"]
    ocr_numbers = [_number(h.get("horse_number"), 28) for h in ocr_horses]
    ocr_names = [_name(h.get("horse_name")) for h in ocr_horses]
    ext_numbers = [_number(h.get("horse_number"), 28) for h in ext_horses]
    ext_names = [_name(h.get("horse_name")) for h in ext_horses]
    number_counts = Counter(number for number in ocr_numbers if number is not None)
    ocr_name_counts = Counter(name for name in ocr_names if name)
    ext_by_number, ext_by_name = defaultdict(list), defaultdict(list)
    for index, (number, name) in enumerate(zip(ext_numbers, ext_names)):
        if number is not None:
            ext_by_number[number].append(index)
        if name:
            ext_by_name[name].append(index)

    reconciliations = []
    pending = []
    final_horses = []
    confirmed_external = set()
    referenced_external = set()
    for index, horse in enumerate(ocr_horses):
        number, name = ocr_numbers[index], ocr_names[index]
        candidates = ext_by_number.get(number, []) if number is not None else []
        name_candidates = ext_by_name.get(name, []) if name else []
        referenced_external.update(candidates)
        referenced_external.update(name_candidates)
        ext_index = candidates[0] if len(candidates) == 1 else None
        ext_horse = ext_horses[ext_index] if ext_index is not None else None
        reasons = []
        if race_check["conflicts"]:
            reasons.append(_reason("race_mismatch", "OCRと外部データのレース情報が一致しません。"))
        if number is None:
            reasons.append(_reason("invalid_ocr_number", "OCR馬番が未設定または不正です。"))
        elif number_counts[number] > 1:
            reasons.append(_reason("duplicate_ocr_number", f"OCR馬番{number}が重複しています。"))
        if number is not None and not candidates:
            reasons.append(_reason("number_not_in_external", f"OCR馬番{number}は外部出馬表に存在しません。"))
        if len(candidates) > 1:
            reasons.append(_reason("duplicate_external_number", f"外部馬番{number}が重複しています。"))
        if not auto_enabled and not name:
            reasons.append(_reason("missing_ocr_name", "OCR馬名が未設定または不明です。"))
        elif not auto_enabled and ocr_name_counts[name] > 1:
            reasons.append(_reason("duplicate_ocr_name", "正規化後のOCR馬名が複数行にあります。"))
        if ext_horse is not None:
            if not ext_names[ext_index]:
                reasons.append(_reason("missing_external_name", "外部馬名が未設定または不明です。"))
            elif not auto_enabled and name and name != ext_names[ext_index]:
                reasons.append(_reason("horse_name_mismatch", "馬番は一致しますが馬名が異なります。類似度では補正しません。"))
            if not auto_enabled and ext_names[ext_index] and len(ext_by_name[ext_names[ext_index]]) > 1:
                reasons.append(_reason("duplicate_external_name", "正規化後の外部馬名が複数行にあります。"))
        confirmed = ext_horse is not None and not reasons
        fields = _entry(horse, ext_horse, confirmed=confirmed, auto_correct=auto_enabled)
        if confirmed:
            confirmed_external.add(ext_index)
            unresolved = [field for field in FIELDS if fields[field]["status"] == "needs_review"]
            if unresolved:
                reasons.append(_reason("unresolved_fields", "未設定または不正な項目を確認してください。", unresolved))
        status = "needs_review" if reasons else "corrected" if any(
            item["status"] in ("corrected", "external_only") for item in fields.values()) else "matched"
        # 補正履歴は確認待ちの理由と分離し、成功時の採点可否を妨げない。
        correction_notes = []
        if auto_enabled and confirmed and status == "corrected":
            changed_fields = [field for field in FIELDS if fields[field]["status"] == "corrected"]
            correction_notes.append(_reason(
                "horse_number_verified_auto_correction",
                "全頭の馬番とレース情報の安全条件を確認し、JRA-VAN値を採用しました。", changed_fields))
        row_id = f"ocr:{index}"
        row = {"row_id": row_id, "ocr_index": index, "external_index": ext_index,
               "external_candidate_indices": list(candidates), "name_candidate_indices": list(name_candidates),
               "identity_confirmed": confirmed, "status": status, "fields": fields,
               "reasons": reasons + correction_notes}
        reconciliations.append(row)
        if reasons:
            pending.append({"row_id": row_id, "ocr_index": index, "external_index": ext_index,
                            "fields": sorted({field for reason in reasons for field in reason["fields"]}),
                            "reasons": deepcopy(reasons)})
        else:
            final_horse = deepcopy(horse)
            for field in FIELDS:
                final_horse[field] = deepcopy(fields[field]["final_value"])
            final_horses.append(final_horse)

    for index, horse in enumerate(ext_horses):
        if index in confirmed_external:
            continue
        reasons = []
        if race_check["conflicts"]:
            reasons.append(_reason("race_mismatch", "OCRと外部データのレース情報が一致しません。"))
        if ext_numbers[index] is None or not ext_names[index]:
            reasons.append(_reason("invalid_external_identity", "外部の馬番または馬名が未設定・不正です。"))
        if ext_numbers[index] is not None and len(ext_by_number[ext_numbers[index]]) > 1:
            reasons.append(_reason("duplicate_external_number", "外部馬番が重複しています。"))
        if ext_names[index] and len(ext_by_name[ext_names[index]]) > 1:
            reasons.append(_reason("duplicate_external_name", "正規化後の外部馬名が重複しています。"))
        status = "needs_review" if reasons or index in referenced_external else "external_only"
        reasons.append(_reason("external_not_confirmed" if status == "needs_review" else "external_only",
                               "対応するOCR行を確定できません。" if status == "needs_review" else
                               "外部出馬表の馬に対応するOCR行がありません。OCR情報の追加または確認が必要です。"))
        fields = _entry(None, horse, confirmed=False)
        if status == "needs_review":
            for item in fields.values():
                item["status"] = "needs_review"
        row_id = f"external:{index}"
        reconciliations.append({"row_id": row_id, "ocr_index": None, "external_index": index,
                                "external_candidate_indices": [index], "name_candidate_indices": [],
                                "identity_confirmed": False, "status": status, "fields": fields,
                                "reasons": reasons})
        pending.append({"row_id": row_id, "ocr_index": None, "external_index": index,
                        "fields": list(FIELDS), "reasons": deepcopy(reasons)})

    if not ocr_horses and not ext_horses:
        pending.append({"row_id": None, "ocr_index": None, "external_index": None,
                        "fields": list(FIELDS), "reasons": [_reason("empty_racecard", "双方の出馬表が空です。") ]})
    final_horses.sort(key=lambda horse: horse["horse_number"])
    counts = dict(Counter(row["status"] for row in reconciliations))
    status = "needs_review" if pending else "corrected" if counts.get("corrected") else "matched"
    return {
        "race": deepcopy(ocr.get("race") or {}),
        "horses": final_horses,
        "status": status,
        "ready_for_scoring": bool(final_horses) and not pending,
        "race_check": race_check,
        "auto_correction": auto_correction,
        "reconciliations": reconciliations,
        "pending_review": pending,
        "summary": {"ocr_count": len(ocr_horses), "external_count": len(ext_horses),
                    "confirmed_count": len(final_horses), "pending_count": len(pending),
                    "status_counts": counts},
        "originals": {"ocr": ocr, "external": external},
    }
