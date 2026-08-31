import os
import streamlit as st
from google import genai
from google.genai import types

st.title("馬柱 ＆ 予想支援アプリ")
st.write("競馬新聞や出馬表の画像をアップロードすると、AIが情報を読み取り、独自のロジックでスコア化します。")

# サイドバーまたは環境変数からAPIキーを取得
api_key = st.sidebar.text_input("Gemini APIキーを入力してください", type="password")

if api_key:
    try:
        # 正しい初期化方法でクライアントを作成
        client = genai.Client(api_key=api_key)

        uploaded_file = st.file_uploader("馬柱の画像を選択してください（スクショや写真）", type=["jpg", "jpeg", "png"])

        if uploaded_file is not None:
            st.image(uploaded_file, caption="アップロードされた馬柱")
            
            if st.button("この馬柱を解析してスコア化する"):
                with st.spinner("AIが馬柱を解析中..."):
                    image_bytes = uploaded_file.getvalue()
                    
                    prompt = """
                    この画像は競馬の出馬表（馬柱）です。
                    記載されている全出走馬について、以下の情報を読み取り、表（マークダウン形式）でまとめてください。
                    
                    1. 馬番
                    2. 馬名
                    3. 騎手
                    4. 調教師
                    5. 近走の傾向（ざっくりとした評価）
                    6. 予想スコア（0〜100点満点で、あなたの独自の視点での期待値を仮に算出してください）
                    
                    最後に、このレースの本命馬を1頭選んで理由を簡潔に述べてください。
                    """

                    response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[
                            types.Part.from_bytes(
                                data=image_bytes,
                                mime_type=uploaded_file.type,
                            ),
                            prompt
                        ]
                    )
                    
                    st.subheader("📊 解析・評価結果")
                    st.markdown(response.text)
                    
    except Exception as e:
        st.error(f"エラーが発生しました: {e}")
else:
    st.info("左上のメニュー（>>）からサイドバーを開き、Gemini API キーを入力してください。")
