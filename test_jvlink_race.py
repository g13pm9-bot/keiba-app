"""JV-Link COMから直近の1レースのRA/SEを読む単独の動作確認用。

実行例: python test_jvlink_race.py --race-date 20261003 --course 05 --race-number 1
引数を省略した項目は対話入力。courseは中央競馬場名または01～10。
仕様: https://jra-van.jp/dlb/sdv/sdk/JV-Link4901.pdf
      https://jra-van.jp/dlb/sdv/sdk/JV-Data4901.pdf
JVReadのBSTRをCP932に戻してから、仕様書の1始まりのバイト位置で切り出す。
騎手名はSEに収録される「騎手名略称」。速報の配信範囲外の過去取得はしない。

インストール済み64bit DLLのIJVLink型情報に対応するpywin32戻り値:
  JVRead:   (result: LONG, buff: BSTR, size: LONG, filename: BSTR)
            buff/size/filenameはすべて[out]のポインタ引数。
  JVGets:   (result: LONG, buff: VARIANT, filename: BSTR)
            buffは[in,out] VARIANT*、sizeは[in] LONG、filenameは[out] BSTR*。
  JVRTOpen: result: LONG（dataspec/keyは[in] BSTR。tupleではない）
このテストはJVReadを使用。JVGetsへの自動切り替えは行わない。
"""

import argparse
from datetime import datetime
import json
import struct
import sys
import time


COURSES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
BUFFER_SIZE = 65536  # RA=1272、SE=555バイト。終端NULLを含め十分確保。


def diagnostic(message):
    # stdoutは指定されたデータだけ。戻り値・理由はstderrに表示する。
    print(message, file=sys.stderr)


def debug_return(method, value, enabled):
    if not enabled:
        return
    diagnostic(f"{method} return type={type(value).__name__}")
    values = value if isinstance(value, tuple) else (value,)
    for index, item in enumerate(values):
        if isinstance(item, (str, bytes)):
            content = f"length={len(item)}, preview={item[:80]!r}"
        else:
            content = repr(item)
        diagnostic(f"  [{index}] type={type(item).__name__}, value={content}")


def return_code(method, value):
    # 型情報上のLONGだけを許可。tuple全体をint()に渡さない。
    if type(value) is not int:
        raise TypeError(f"{method}: LONGを期待しましたが{type(value).__name__}が返りました。"
                        "--debugで戻り値を確認してください。")
    return value


def read_record(jv, debug=False):
    # 型情報に沿った通常引数で呼び、[out]はpywin32のtupleから取得する。
    # JVReadではsizeも[out]なので4要素。JVGetsの3要素とは区別する。
    returned = jv.JVRead(" " * BUFFER_SIZE, BUFFER_SIZE, "")
    debug_return("JVRead", returned, debug)
    if not isinstance(returned, tuple) or len(returned) != 4:
        raise TypeError("JVRead: (result, buff, size, filename)の4要素tupleを期待しました。"
                        "--debugで戻り値を確認してください。")
    code, text, returned_size, filename = returned
    code = return_code("JVRead.result", code)
    # EOF/境界/エラー時はbuff等が未設定でもよい。正常レコードだけ検証する。
    if code > 0:
        if not isinstance(text, str) or type(returned_size) is not int or not isinstance(filename, str):
            raise TypeError("JVRead: buff/size/filenameの型がBSTR/LONG/BSTRと一致しません。")
        if code >= BUFFER_SIZE:
            raise ValueError(f"JVRead: バッファ容量を超える戻り値です: {code}")
    return code, text


def field(record, position, length):
    start = position - 1
    if len(record) < start + length:
        raise ValueError(f"レコード不足: 位置{position}、長さ{length}、実長{len(record)}")
    return record[start:start + length].decode("cp932", errors="strict").strip(" \u3000")


def parameters():
    parser = argparse.ArgumentParser(description="現在配信中の1レースの出馬表をJV-Linkから取得")
    parser.add_argument("--race-date", help="開催日 YYYYMMDD または YYYY-MM-DD")
    parser.add_argument("--course", help="競馬場名または01～10の競馬場コード")
    parser.add_argument("--race-number", help="レース番号 1～12")
    parser.add_argument("--debug", action="store_true", help="COM戻り値の型・要素・先頭80文字をstderrに表示")
    args = parser.parse_args()
    date = (args.race_date or input("race_date (YYYYMMDD): ")).strip().replace("-", "")
    if len(date) != 8 or not date.isascii() or not date.isdecimal():
        raise ValueError("race_dateはYYYYMMDDまたはYYYY-MM-DDで指定してください。")
    datetime.strptime(date, "%Y%m%d")
    course = (args.course or input("course (競馬場名 / 01～10): ")).strip()
    names = {name: code for code, name in COURSES.items()}
    course = names.get(course, course.zfill(2))
    if course not in COURSES:
        raise ValueError("course: " + ", ".join(f"{code}={name}" for code, name in COURSES.items()))
    race = int(args.race_number or input("race_number (1～12): "))
    if not 1 <= race <= 12:
        raise ValueError("race_numberは1～12で指定してください。")
    return date, course, f"{race:02d}", args.debug


def fetch(jv, date, course, race, debug=False):
    returned = jv.JVInit("UNKNOWN")
    debug_return("JVInit", returned, debug)
    code = return_code("JVInit", returned)
    diagnostic(f"JVInit result = {code}")
    if code != 0:
        diagnostic("初期化に失敗しました。取得は行いません。")
        return None

    # 12桁キー。回次・日次を入力せず、1レースを直接指定できる公式形式。
    returned = jv.JVRTOpen("0B15", date + course + race)
    debug_return("JVRTOpen", returned, debug)
    code = return_code("JVRTOpen", returned)
    diagnostic(f"JVRTOpen result = {code}")
    if code != 0:
        if code == -1:
            diagnostic("該当データなし、または更新版のダウンロード選択で終了しました。"
                       "未配信・開催のない日・速報提供範囲外の可能性があります。")
        else:
            diagnostic("取得要求に失敗しました。上記JV-Linkコードを確認してください。"
                       "認証・通信等のエラーをデータなしとは扱いません。")
        return None

    ra = None
    horses = {}
    deadline = time.monotonic() + 60
    while True:
        if time.monotonic() >= deadline:
            diagnostic("読み込みを60秒で打ち切りました。完全な結果を確認できないため表示しません。")
            return None
        code, text = read_record(jv, debug)
        if code == 0:
            break
        if code == -1:  # ファイル境界。取得要求の-1（該当なし）とは意味が異なる。
            continue
        if code < 0:
            diagnostic(f"JVRead result = {code}: 読み込み失敗。部分結果は表示しません。")
            return None
        # 置換・無視を行わず、CP932で元の固定長バイト列に戻す。
        raw = text.encode("cp932", errors="strict").rstrip(b"\x00")
        if len(raw) != code:
            raise ValueError(f"JVReadのバイト数不一致: result={code}, CP932={len(raw)}")
        kind = raw[:2]
        if kind not in (b"RA", b"SE"):
            continue
        expected = 1272 if kind == b"RA" else 555
        if len(raw) != expected or not raw.endswith(b"\r\n"):
            raise ValueError(f"{kind.decode('ascii')}の固定長/CRLFが仕様と一致しません。")
        identity = (field(raw, 12, 8), field(raw, 20, 2), field(raw, 26, 2))
        if identity != (date, course, race):
            continue
        state = field(raw, 3, 1)
        if kind == b"RA":
            ra = None if state == "0" else raw
        else:
            # 馬番未確定の出走馬名表も別々に保持するため血統登録番号をキーにする。
            horse_id = field(raw, 31, 10)
            if state == "0":
                horses.pop(horse_id, None)
            else:
                horses[horse_id] = raw

    if ra is None or not horses:
        diagnostic("対象レースの有効なRA/SEがそろいません。"
                   "未配信・削除済み・提供範囲外等が考えられますが、応答だけでは原因を断定できません。")
        return None
    if field(ra, 3, 1) == "9":
        diagnostic("対象レースはレース中止（RAデータ区分9）です。")
        return None
    count = int(field(ra, 882, 2))
    if count != len(horses):
        diagnostic(f"RA登録頭数={count}、SE件数={len(horses)}で一致しません。"
                   "速報の更新途中等の可能性があるため、完全な出馬表として表示しません。")
        return None
    rows = []
    for raw in horses.values():
        number = field(raw, 29, 2)
        frame = field(raw, 28, 1)
        rows.append({
            "horse_number": int(number) if number.isdecimal() and int(number) else None,
            "horse_name": field(raw, 41, 36),
            "frame_number": int(frame) if frame.isdecimal() and int(frame) else None,
            "jockey_name": field(raw, 307, 8),
        })
    rows.sort(key=lambda row: (row["horse_number"] or 99, row["horse_name"]))
    if any(row["horse_number"] is None or row["frame_number"] is None for row in rows):
        diagnostic("馬番・枠番に未確定の値があります（出走馬名表段階等）。未確定はnullで表示します。")
    return {
        "race_date": field(ra, 12, 8),
        "course": COURSES[field(ra, 20, 2)],
        "race_number": int(field(ra, 26, 2)),
        "horses": rows,
    }


def main():
    jv = None
    pythoncom = None
    initialized = False
    result = None
    status = 1
    debug = False
    try:
        date, course, race, debug = parameters()
        if struct.calcsize("P") * 8 != 64:
            raise ValueError("64bit Pythonから実行してください。")
        import pythoncom as com
        from win32com.client import dynamic

        pythoncom = com
        pythoncom.CoInitialize()
        initialized = True
        jv = dynamic.Dispatch("JVDTLab.JVLink")
        result = fetch(jv, date, course, race, debug)
        status = 0 if result is not None else 1
    except (KeyboardInterrupt, EOFError):
        diagnostic("入力/取得を中止しました。")
    except Exception as exc:
        hresult = getattr(exc, "hresult", None)
        if hresult is not None:
            diagnostic(f"COM error HRESULT={hresult} (0x{hresult & 0xffffffff:08X}): {exc}")
        else:
            diagnostic(f"確認失敗: {type(exc).__name__}: {exc}")
    finally:
        # COM作成後は初期化/取得の失敗・Ctrl+Cでも必ずJVCloseを呼ぶ。
        if jv is not None:
            try:
                returned = jv.JVClose()
                debug_return("JVClose", returned, debug)
                code = return_code("JVClose", returned)
                diagnostic(f"JVClose result = {code}")
                if code != 0:
                    status = 1
            except Exception as exc:
                diagnostic(f"JVClose失敗: {exc}")
                status = 1
        jv = None
        if initialized:
            pythoncom.CoUninitialize()
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
