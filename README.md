# 電力会社 停電情報 → Discord 速報通知

大手電力10社の公式「停電情報」ページを **5分ごと** に巡回し、停電の発生・復旧・拡大を検知したときだけ Discord に通知します。GitHub Actions 上で動くので、あなたのPCやアプリを開いていなくても24時間動き続けます。

状態が変化したときだけ通知する **エッジトリガ方式** なので、5分ごとに実行してもスパムになりません。

---

## セットアップ手順（15分ほど）

### 1. GitHubリポジトリを用意する
1. GitHub にログイン →「New repository」
2. 名前は任意（例 `power-outage-discord`）。**Private を推奨**
3. このフォルダの中身をすべてアップロード（`monitor.py` / `requirements.txt` / `state.json` / `.github/workflows/monitor.yml` / `README.md`）
   - 画面から入れる場合は「uploading an existing file」でドラッグ＆ドロップ。`.github` フォルダも忘れずに（フォルダごとドラッグするか、`.github/workflows/monitor.yml` というパスでファイルを作成）

### 2. Discord Webhook URL を Secret に登録する
1. リポジトリの **Settings → Secrets and variables → Actions**
2. **New repository secret**
3. Name: `DISCORD_WEBHOOK_URL`
4. Secret: あなたの Webhook URL を貼り付け → **Add secret**

> ⚠️ Webhook URL はコードに直接書かず、必ず Secret に入れてください。リポジトリを公開していても URL は漏れません。

### 3. Actions を有効化する
1. リポジトリの **Actions** タブを開く
2. ワークフローを有効化（初回は「I understand my workflows, go ahead and enable them」を押す）
3. 「停電情報モニター」を選び、**Run workflow**（workflow_dispatch）で手動実行してテスト

うまく動くと、状態変化があった会社の分だけ Discord に通知が届きます。初回は「停電なし」の会社が多いので通知は基本ありません（テストしたい場合は下の「動作テスト」参照）。

以降は cron により **5分ごと** に自動実行されます。

---

## 通知の種類

| 表示 | 意味 |
|------|------|
| ⚡ 新規停電 | 停電なし → 発生 を検知 |
| 🔺 停電拡大 | 停電戸数が大きく増加（既定: +1000戸 かつ +50%以上） |
| 🔻 停電縮小 | 停電戸数が大きく減少 |
| ✅ 復旧 | 発生 → 停電なし を検知 |

各通知には会社名・停電エリア/戸数・公式ページへのリンクが入ります。

---

## 各社の対応状況（重要）

停電情報ページの作りが会社ごとに違うため、取得の確実さに2段階あります。

### ✅ 確実（HTMLに直接データ。requestsで取得）
- 北海道電力ネットワーク
- 北陸電力送配電
- 関西電力送配電
- 中国電力ネットワーク
- 四国電力送配電

これらは「停電なし」の文言や戸数表示を実ページで確認済みで、検知ロジックも実データ由来のサンプルでテスト済みです。

### ⚠️ ベストエフォート（JavaScript描画。Playwrightで取得）
- 東北電力ネットワーク
- 東京電力パワーグリッド
- 中部電力パワーグリッド
- 九州電力送配電
- 沖縄電力

これらはページが JavaScript で地図を描画するため、ヘッドレスブラウザ（Playwright）で描画してからテキストを読み取ります。**実際に停電が起きている状態の表示文言を確認できていない**ため、検知は「県名＋戸数」や停電を示す語がある場合のみ発生と判定する安全側の設定です（誤通知でスパムするより取りこぼしを許容）。

**とくに東京電力（TEPCO）** は停電状況を画像（canvas）で描画しており、テキストとして読み取れない可能性があります。実運用で最初の台風・大規模停電時にログを確認し、必要なら該当社の検知関数（`monitor.py` の `detect_*` / `detect_generic_js`）を調整してください。各社の判定結果は Actions の実行ログ（`[OK] 会社名: outage=... hh=...`）で毎回確認できます。

---

## 動作テスト（通知が届くか確かめたい）

`state.json` を手で書き換えると擬似的に「状態変化」を作れます。たとえば北海道を「発生中だった」ことにして次回実行で「復旧」通知を出す:

```json
{ "hokkaido": { "has_outage": true, "households": 500 } }
```

を commit → 次の実行（または Run workflow）で `✅ 復旧｜北海道電力ネットワーク` が届きます。確認後 `state.json` を `{}` に戻してください。

---

## ローカルで試す（任意）

```bash
pip install -r requirements.txt
python -m playwright install chromium
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python monitor.py
```

> 注意: この通信には外部ネットワークが必要です。制限された環境では動きません。

---

## 設定のカスタマイズ

`monitor.py` 冒頭の定数、または Actions の環境変数で調整できます。

- `NOTIFY_RESTORE=0` … 復旧通知をオフ
- `CHANGE_ABS` / `CHANGE_RATIO` … 拡大・縮小を通知する戸数のしきい値
- 監視社を減らす … `PROVIDERS` リストから該当行を削除

実行間隔を変えるなら `.github/workflows/monitor.yml` の `cron` を編集（例: 10分ごとは `*/10 * * * *`）。

> GitHub Actions の cron は混雑時に数分遅れて起動することがあります（GitHubの仕様）。厳密な5分間隔が保証されるわけではない点はご了承ください。無料枠でも十分収まる想定です。

---

## 仕組み

```
GitHub Actions (5分ごと)
   └─ monitor.py
        ├─ 各社ページを取得（requests / Playwright）
        ├─ 停電の有無・戸数を判定
        ├─ 前回状態(state.json)と比較
        ├─ 変化した会社だけ Discord Webhook に投稿
        └─ state.json を更新してコミット
```
