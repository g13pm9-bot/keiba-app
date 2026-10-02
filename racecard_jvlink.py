"""JV-Link COMによる1レースの出馬表取得。importだけではCOMを起動しない。

公開関数: fetch_racecard(race_date, course, race_number)
現在配信中の速報レース情報(0B15)を使用する。
JVReadのtupleとCP932固定長解析はtest_jvlink_race.pyの成功した方法を踏襲。
騎手名はSEの「騎手名略称」。データの保存・画面表示は呼び出し側で行う。
"""

from datetime import datetime
import struct
import time
import warnings


COURSES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
BUFFER_SIZE = 65536
READ_TIMEOUT_SECONDS = 60


class JVLinkError(RuntimeError):
    """取得失敗。method/code/hresult/details/cleanup_errorを参照できる。"""

    def __init__(self, message, *, method, code=None, hresult=None, details=None):
        self.method = method
        self.code = code
        self.hresult = hresult
        self.details = details or {}
        self.cleanup_error = None
        context = f"{method}: {message}"
        if code is not None:
            context += f" (JV-Link code={code})"
        if hresult is not None:
            context += f" (HRESULT=0x{hresult & 0xffffffff:08X})"
        super().__init__(context)


class JVLinkNoDataError(JVLinkError):
    """該当データなし、または有効なRA/SEがそろっていない。"""


class RacecardWarning(UserWarning):
    """warnings.catch_warningsで収集可能。kind/detailsに構造化情報を保持。"""

    def __init__(self, message, *, kind, details=None):
        self.kind = kind
        self.details = details or {}
        super().__init__(message)


def _normalize_input(race_date, course, race_number):
    if not isinstance(race_date, str):
        raise ValueError("race_dateはYYYYMMDDまたはYYYY-MM-DDの文字列で指定してください。")
    date = race_date.strip()
    if len(date) == 10 and date[4] == "-" and date[7] == "-":
        date = date[:4] + date[5:7] + date[8:]
    if len(date) != 8 or not date.isascii() or not date.isdecimal():
        raise ValueError("race_dateはYYYYMMDDまたはYYYY-MM-DDで指定してください。")
    datetime.strptime(date, "%Y%m%d")
    if type(course) not in (str, int):
        raise ValueError("courseは競馬場名または中央競馬場コード01～10で指定してください。")
    course_text = str(course).strip()
    names = {name: code for code, name in COURSES.items()}
    code = names.get(course_text, course_text.zfill(2))
    if code not in COURSES:
        raise ValueError("course: " + ", ".join(f"{key}={name}" for key, name in COURSES.items()))
    if type(race_number) not in (str, int):
        raise ValueError("race_numberは1～12の整数または数字文字列で指定してください。")
    race_text = str(race_number).strip()
    if not race_text.isascii() or not race_text.isdecimal() or not 1 <= int(race_text) <= 12:
        raise ValueError("race_numberは1～12で指定してください。")
    return date, code, f"{int(race_text):02d}"


def _field(record, position, length):
    # 仕様書の1始まりの「バイト位置」。Unicode文字列はスライスしない。
    start = position - 1
    if len(record) < start + length:
        raise ValueError(f"レコード不足: 位置{position}、長さ{length}、実長{len(record)}")
    return record[start:start + length].decode("cp932", errors="strict").strip(" \u3000")


def _call(jv, method, *args):
    try:
        return getattr(jv, method)(*args)
    except Exception as exc:
        raise JVLinkError(
            str(exc), method=method, hresult=getattr(exc, "hresult", None),
        ) from exc


def _return_code(method, value):
    if type(value) is not int:
        raise JVLinkError(
            f"LONGの戻り値を期待しましたが{type(value).__name__}が返りました。",
            method=method,
        )
    return value


def _read_record(jv):
    returned = _call(jv, "JVRead", " " * BUFFER_SIZE, BUFFER_SIZE, "")
    # インストール済みIJVLink型情報: JVReadはsizeも[out]で4要素。
    # JVGetsは(result, buff, filename)の3要素だが、本モジュールでは使用しない。
    if not isinstance(returned, tuple) or len(returned) != 4:
        raise JVLinkError(
            "(result, buff, size, filename)の4要素tupleではありません。",
            method="JVRead", details={"return_type": type(returned).__name__},
        )
    code, text, returned_size, filename = returned
    code = _return_code("JVRead", code)
    if code > 0:
        if not isinstance(text, str) or type(returned_size) is not int or not isinstance(filename, str):
            raise JVLinkError("buff/size/filenameの型がBSTR/LONG/BSTRと一致しません。", method="JVRead")
        if code >= BUFFER_SIZE:
            raise JVLinkError("バッファ容量を超える戻り値です。", method="JVRead", code=code)
    return code, text


def _horse_number(record, position, length, maximum, label):
    value = _field(record, position, length)
    if not value or value == "0" * length:
        return None
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= maximum:
        raise JVLinkError(f"{label}が不正です: {value!r}", method="parse")
    return int(value)


def _collect(jv, target):
    date, course, race = target
    code = _return_code("JVInit", _call(jv, "JVInit", "UNKNOWN"))
    if code != 0:
        raise JVLinkError("初期化に失敗しました。", method="JVInit", code=code)
    code = _return_code("JVRTOpen", _call(jv, "JVRTOpen", "0B15", date + course + race))
    if code == -1:
        raise JVLinkNoDataError(
            "該当データなし、または更新版のダウンロード選択による終了です。"
            "未配信・開催のない日・速報提供範囲外の可能性があります。",
            method="JVRTOpen", code=code,
        )
    if code != 0:
        raise JVLinkError("取得要求に失敗しました。認証・通信等の状態を確認してください。",
                          method="JVRTOpen", code=code)

    ra = None
    horses = {}
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise JVLinkError("読み込みループが60秒を超えました。部分結果は返しません。", method="JVRead")
        code, text = _read_record(jv)
        if code == 0:
            break
        if code == -1:  # ファイル境界。JVRTOpenの-1とは区別。
            continue
        if code < 0:
            raise JVLinkError("読み込みに失敗しました。部分結果は返しません。", method="JVRead", code=code)
        raw = text.encode("cp932", errors="strict").rstrip(b"\x00")
        if len(raw) != code:
            raise JVLinkError("JVReadの戻り値とCP932バイト数が一致しません。", method="parse",
                              details={"read_bytes": code, "cp932_bytes": len(raw)})
        kind = raw[:2]
        if kind not in (b"RA", b"SE"):
            continue
        expected = 1272 if kind == b"RA" else 555
        if len(raw) != expected or not raw.endswith(b"\r\n"):
            raise JVLinkError(f"{kind.decode('ascii')}の固定長/CRLFが仕様と一致しません。", method="parse")
        identity = (_field(raw, 12, 8), _field(raw, 20, 2), _field(raw, 26, 2))
        if identity != target:
            raise JVLinkError("RA/SEのレース日付・競馬場・番号が取得対象と一致しません。",
                              method="validate", details={"expected": target, "actual": identity})
        state = _field(raw, 3, 1)
        if kind == b"RA":
            ra = None if state == "0" else raw
        else:
            horse_id = _field(raw, 31, 10)
            if not horse_id or horse_id == "0" * 10:
                raise JVLinkError("SEの血統登録番号が未設定です。", method="validate")
            # テストと同様、同一馬の更新は置換し、削除レコードは取り除く。
            if state == "0":
                horses.pop(horse_id, None)
            else:
                horses[horse_id] = raw

    if ra is None or not horses:
        raise JVLinkNoDataError(
            "対象の有効なRA/SEがそろいません。未配信・削除済み・提供範囲外等が考えられます。",
            method="validate", details={"ra_present": ra is not None, "se_count": len(horses)},
        )
    if _field(ra, 3, 1) == "9":
        raise JVLinkError("対象レースは中止です（RAデータ区分9）。", method="validate")

    count = int(_field(ra, 882, 2))
    rows = []
    numbers = set()
    for raw in horses.values():
        number = _horse_number(raw, 29, 2, 28, "馬番")
        frame = _horse_number(raw, 28, 1, 8, "枠番")
        if number is not None:
            if number in numbers:
                raise JVLinkError(f"馬番{number}が複数の馬に重複しています。", method="validate",
                                  details={"duplicate_horse_number": number})
            numbers.add(number)
        rows.append({
            "horse_number": number,
            "horse_name": _field(raw, 41, 36),
            "frame_number": frame,
            "jockey_name": _field(raw, 307, 8),
        })

    if len(numbers) == len(rows):
        # 観測最大馬番とRA登録頭数を基準に欠番候補を通知。未配信とは断定しない。
        missing = sorted(set(range(1, max(count, max(numbers)) + 1)) - numbers)
        if missing:
            warnings.warn(RacecardWarning(
                f"馬番に欠番があります: {missing}。取消・削除・更新途中等の可能性があります。",
                kind="missing_horse_numbers", details={"missing_numbers": missing},
            ), stacklevel=3)
    if count != len(rows):
        raise JVLinkError("RA登録頭数とSE件数が一致しません。完全な出馬表として返しません。",
                          method="validate", details={"registered_count": count, "se_count": len(rows)})
    if any(row["horse_number"] is None or row["frame_number"] is None for row in rows):
        warnings.warn(RacecardWarning(
            "馬番・枠番に未確定の値があります。Noneで返します。未確定の馬番がある場合は欠番を確定できません。",
            kind="unassigned_numbers",
        ), stacklevel=3)
    rows.sort(key=lambda row: (row["horse_number"] is None, row["horse_number"] or 0, row["horse_name"]))
    return {
        "race": {
            "race_date": _field(ra, 12, 8),
            "course": COURSES[_field(ra, 20, 2)],
            "race_number": int(_field(ra, 26, 2)),
        },
        "horses": rows,
    }


def fetch_racecard(race_date, course, race_number):
    """現在配信中の1レースを取得し、{'race': {...}, 'horses': [...]}を返す。

    race_date: 'YYYYMMDD' / 'YYYY-MM-DD'
    course: 競馬場名 / '01'～'10' / 1～10
    race_number: 1～12の整数または数字文字列
    欠番等はRacecardWarning。入力不正はValueError、取得失敗はJVLinkError。
    呼び出したスレッドでCOMの初期化・解放を行い、COMオブジェクトは返さない。
    60秒制限は読み込みループ用で、COMメソッド自体の通信タイムアウトではない。
    """
    target = _normalize_input(race_date, course, race_number)
    if struct.calcsize("P") * 8 != 64:
        raise JVLinkError("64bit Pythonから呼び出してください。", method="environment")
    try:
        import pythoncom
        from win32com.client import dynamic
    except ImportError as exc:
        raise JVLinkError("pywin32を読み込めません。", method="environment") from exc

    jv = None
    initialized = False
    primary_error = None
    try:
        pythoncom.CoInitialize()
        initialized = True
        jv = dynamic.Dispatch("JVDTLab.JVLink")
        return _collect(jv, target)
    except JVLinkError as exc:
        primary_error = exc
        raise
    except Exception as exc:
        primary_error = JVLinkError(str(exc), method="COM/parse", hresult=getattr(exc, "hresult", None))
        raise primary_error from exc
    except BaseException as exc:
        # Ctrl+C等の中断も、クローズ失敗で元の例外を置き換えない。
        primary_error = exc
        raise
    finally:
        cleanup_error = None
        try:
            if jv is not None:
                try:
                    code = _return_code("JVClose", _call(jv, "JVClose"))
                    if code != 0:
                        raise JVLinkError("クローズに失敗しました。", method="JVClose", code=code)
                except Exception as exc:
                    cleanup_error = exc
        finally:
            jv = None
            if initialized:
                try:
                    pythoncom.CoUninitialize()
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = JVLinkError(str(exc), method="CoUninitialize")
                    else:
                        cleanup_error.add_note(f"CoUninitializeも失敗: {exc}")
        if cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(f"後処理も失敗: {cleanup_error}")
                if isinstance(primary_error, JVLinkError):
                    primary_error.cleanup_error = cleanup_error
            else:
                raise cleanup_error
