# -*- coding: utf-8 -*-
"""不足情報の撮影案内と追加画像の読み取り。採点は既存アプリに任せる。"""
from copy import deepcopy

import streamlit as st


GUIDANCE = {
    "過去走": "馬番・馬名と、近走の着順・頭数・日付が一緒に読める写真をお願いします。文字がぼやけている場合は近づいて撮影し直してください。",
    "調教": "馬番・馬名と、調教コース・時計が読める拡大写真をお願いします。",
    "騎手成績": "馬番・馬名と、騎手の勝率・複勝率などが載った写真をお願いします。",
    "血統": "馬番・馬名と、父・母父が読める写真をお願いします。",
    "オッズ": "馬番・馬名と、オッズが一緒に読める写真をお願いします。",
    "脚質": "脚質が紙面に記載されていれば、ページ上部の①全体画像に追加して解析ボタンを押してください。",
    "枠順": "枠番・馬番・馬名の対応が読める写真を、ページ上部の①全体画像に追加して解析ボタンを押してください。",
    "馬の識別": "馬番・馬名が読める写真を、ページ上部の①全体画像に追加して解析ボタンを押してください。",
}


def missing_rows(data, coverage, number):
    rows = []
    for horse in data.get("horses") or []:
        missing = list(coverage(horse)[2])
        if number(horse.get("odds")) is None:
            missing.append("オッズ")
        if horse.get("horse_number") is None or not horse.get("horse_name") or horse.get("horse_name") == "不明":
            missing.append("馬の識別")
        for item in missing:
            rows.append({"馬番": horse.get("horse_number"), "馬名": horse.get("horse_name") or "不明",
                         "不足情報": item, "撮影の案内": GUIDANCE.get(item, "該当欄が読める別の写真をお願いします。")})
    return rows


def recover_data(data, meta, groups, operations, client, extract, identity, finalize):
    """成功時だけ呼び出し側が採用するコピーを返す。失敗時は元データを保つ。"""
    updated, updated_meta = deepcopy(data), deepcopy(meta)
    context = identity(updated.get("horses") or [])
    for category, files in groups.items():
        prompt, schema, merge = operations[category]
        for index, photo in enumerate(files, 1):
            partial = extract(client, f"recovery_{category}_{index}", prompt, schema,
                              [photo], context, True)
            merge(updated, partial)
            key = f"{category}_calls"
            updated_meta[key] = updated_meta.get(key, 0) + 1
            updated_meta["total_calls"] = updated_meta.get("total_calls", 0) + 1
    return finalize(updated), updated_meta


def show_recovery(data, meta, signature, cache_key, api_key, coverage, number,
                  identity, extract, finalize, operations, client_factory):
    rows = missing_rows(data, coverage, number)
    race = data.get("race") or {}
    missing_race = [label for key, label in (("date", "レース日"), ("course", "競馬場"),
                    ("race_number", "レース番号"), ("surface", "芝・ダート"), ("distance_m", "距離"))
                    if race.get(key) in (None, "")]
    message_key = f"recovery_message_{signature}"
    if message_key in st.session_state:
        st.info(st.session_state.pop(message_key))
    if not rows and not missing_race:
        return
    st.subheader("📷 不足情報を追加する")
    st.warning("読み取れていない情報があります。該当欄を撮影し直すか、別の写真を追加してください。")
    st.caption("紙面に記載がない情報や、新馬などで存在しない過去走は追加不要です。追加せずに現在の結果を確認・保存することもできます。")
    if missing_race:
        st.info("レース情報の不足：" + "・".join(missing_race) + "。レース見出しが読める写真をページ上部の①全体画像に追加して、解析ボタンを押してください。")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    with st.expander("追加写真から不足情報を再取得する", expanded=False):
        st.write("今の結果と同じレースの写真を追加してください。馬番・馬名を含め、反射や影を避けて撮影してください。")
        groups = {}
        for category, label in (("previous", "近走成績の追加写真"), ("training", "調教の追加写真"),
                                ("extra", "血統・騎手成績・オッズの追加写真")):
            groups[category] = st.file_uploader(label, type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=True, key=f"recovery_{signature}_{category}") or []
        if st.button("追加写真を読み取って結果を更新", key=f"recover_{signature}",
                     disabled=not api_key or not any(groups.values())):
            try:
                with st.spinner("追加写真を読み取っています…"):
                    updated, updated_meta = recover_data(data, meta, groups, operations,
                        client_factory(api_key=api_key), extract, identity, finalize)
                remaining = len(missing_rows(updated, coverage, number))
                resolved = max(0, len(rows) - remaining)
                st.session_state[cache_key] = {"data": updated, "meta": updated_meta}
                st.session_state[message_key] = (
                    f"追加写真を読み取り、不足項目が{resolved}件解消しました。残り{remaining}件です。更新後の結果を保存する場合は保存ボタンを押してください。"
                    if resolved else "追加写真を読み取りましたが、不足項目数は減りませんでした。該当欄がより鮮明な別の写真をお願いします。元々記載がない情報は追加不要です。"
                )
            except Exception as exc:
                st.error(f"追加写真を読み取れませんでした。現在の結果は保持しています。再度お試しください：{exc}")
            else:
                st.rerun()
