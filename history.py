# -*- coding: utf-8 -*-
import os
import csv
from pathlib import Path
import streamlit as st
import pandas as pd

CSV_FILE = Path(__file__).resolve().parent / "prediction_history.csv"

def save_prediction(race_info, results):
    """予想結果をCSVファイルに保存する"""
    headers = [
        "レース日", "競馬場", "レース番号", "馬番", "馬名",
        "能力", "適性", "調教", "展開", "騎手", "血統", "枠順",
        "総合点", "印", "オッズ"
    ]
    
    # ファイルが存在するかどうかをチェック（ヘッダー書き込みの判定用）
    if not results:
        raise ValueError("保存する予想結果がありません。")
    file_exists = os.path.isfile(CSV_FILE) and os.path.getsize(CSV_FILE) > 0
    if file_exists:
        with open(CSV_FILE, encoding="utf-8-sig", newline="") as existing:
            if next(csv.reader(existing), None) != headers:
                raise ValueError("既存CSVの列が一致しません。履歴ファイルを確認してください。")
    
    # utf-8-sig で保存（Excelで開いたときの文字化け防止）
    with open(CSV_FILE, mode='a', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
            
        date = race_info.get("date") or ""
        course = race_info.get("course") or ""
        race_num = race_info.get("race_number") or ""
        
        for r in results:
            writer.writerow({
                "レース日": date,
                "競馬場": course,
                "レース番号": race_num,
                "馬番": r.get("horse_number", ""),
                "馬名": r.get("horse_name", ""),
                "能力": r.get("ability", ""),
                "適性": r.get("suitability", ""),
                "調教": r.get("training", ""),
                "展開": r.get("pace", ""),
                "騎手": r.get("jockey", ""),
                "血統": r.get("pedigree", ""),
                "枠順": r.get("gate", ""),
                "総合点": r.get("total", ""),
                "印": r.get("mark", ""),
                "オッズ": r.get("odds", "")
            })

def show_history():
    """Streamlit画面に予想履歴を一覧表示する"""
    st.subheader("📖 過去の予想履歴")
    st.write("これまでに保存された予想履歴の一覧です。")
    
    if not os.path.isfile(CSV_FILE) or os.path.getsize(CSV_FILE) == 0:
        st.info("保存された予想履歴はまだありません。「新規予想」画面から結果を保存してください。")
        return
        
    try:
        df = pd.read_csv(CSV_FILE, encoding="utf-8-sig", keep_default_na=False)
        # 履歴をデータフレームで表示
        st.dataframe(df, use_container_width=True, hide_index=True)
        
        # CSVダウンロードボタン
        with open(CSV_FILE, "rb") as f:
            st.download_button(
                label="📥 履歴CSVをダウンロード",
                data=f,
                file_name="prediction_history.csv",
                mime="text/csv"
            )
    except Exception as e:
        st.error(f"履歴の読み込み中にエラーが発生しました: {e}")
