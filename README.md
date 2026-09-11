[README.md](https://github.com/user-attachments/files/32095181/README.md)
# 馬柱＆予想支援アプリ v4.1

v4で発生した **Schema validation error** を修正した版です。

## v4.1の主な修正

- `response_schema` から `response_json_schema` に変更
- nullable項目を `anyOf` で定義
- `running_style` の `null` 許容方法を修正
- Google Gen AI SDKを新しいバージョンに固定
- AIは画像から事実抽出だけ
- 採点、順位、印、期待値はPython側で固定計算

## GitHubで置き換えるファイル

- `app.py`
- `requirements.txt`
- `README.md`

`scoring_rules.md` はv4のものをそのまま使えます。

## Streamlit Cloud

Secretsに以下を設定してください。

```toml
GEMINI_API_KEY = "実際のAPIキー"
```

APIキーはGitHubへアップロードしないでください。

## テスト方法

1. 同じ馬柱画像をアップロード
2. 「解析 → 固定ルール採点」
3. 下部の「Geminiが読み取った生データ」を確認
4. 同じ画像でもう一度実行
5. 生データと採点結果の再現性を比較

採点部分は同じ入力データなら同じ結果になります。
