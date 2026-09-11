[README.md](https://github.com/user-attachments/files/32092995/README.md)
# 馬柱＆予想支援アプリ

## 特徴
- Geminiは画像から事実を抽出するだけ
- 採点はPythonの固定ルールで実施
- 同じ画像・同じ入力なら採点結果が基本的に同じになる設計
- 休み明け、叩き2走目、叩き3走目を考慮
- 100点満点の内訳を表示
- AI抽出データをJSONで確認可能

## 起動

```bash
pip install -r requirements.txt
streamlit run app.py
```

Gemini APIキーは `.streamlit/secrets.toml` に以下の形式で設定できます。

```toml
GEMINI_API_KEY = "あなたのAPIキー"
```

または環境変数 `GEMINI_API_KEY` を利用できます。
