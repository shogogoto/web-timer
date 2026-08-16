# セルフホスト集中タイマー

集中して取り組んだ時間を記録する、小さなセルフホストWebタイマーです。

個人の勉強・読書・作業用としても、家族や小さなグループで共有するタイマーとしても利用できます。業務用タイムトラッカーのような案件管理や請求機能は持たず、カウントダウンと実際に取り組んだ時間の記録に集中しています。

## 主な機能

- 5分・10分・15分・30分・45分・60分のプリセットと5分単位の調整
- ワンクリックでの開始、一時停止、再開、途中終了
- タブやブラウザを閉じても継続するサーバー基準のタイマー
- 終了音、ブラウザ通知、Web Push通知
- 毎日または曜日・時刻を指定し、任意メッセージも設定できるWeb Pushリマインダー（開始は手動）
- 利用者ごとのタイマー設定保持
- 今日・週間グラフ・月間カレンダー・時間帯別グラフ
- 管理者によるアカウント作成、パスワード再設定、利用者別集計
- 報告用集計テキストのコピー
- SQLiteによる単一ディレクトリのデータ管理

## Docker Composeで起動

```bash
git clone https://github.com/shogogoto/web-timer.git
cd web-timer
cp .env.example .env
```

`.env`の`ADMIN_PASSWORD`、`SESSION_SECRET`、`VAPID_SUBJECT`を変更します。秘密値は例えば次のように生成できます。

```bash
openssl rand -hex 32
```

公開済みGHCRイメージを使う場合:

```bash
docker compose pull
docker compose up -d
```

ソースからローカルビルドする場合:

```bash
docker compose up -d --build
```

標準では`http://localhost:8000/login`で開けます。初回起動時に`.env`の管理者アカウントが作成されるので、管理画面から通常の利用者アカウントを追加してください。個人利用の場合も自分用の利用者アカウントを1つ作成します。

> GHCRパッケージが公開されるまでは、ローカルビルドで起動してください。

## ポート番号の変更

ホスト側のポートは`.env`で変更できます。コンテナ内では常に8000番を使用します。

```dotenv
TIMER_PORT=8080
```

この場合は`http://localhost:8080/login`で開きます。別のイメージや固定バージョンを使う場合は`TIMER_IMAGE`を変更します。

```dotenv
TIMER_IMAGE=ghcr.io/shogogoto/web-timer:0.1.0
```

## 環境変数

| 名前 | 標準値 | 説明 |
|---|---|---|
| `TIMER_PORT` | `8000` | ホスト側で公開するポート |
| `TIMER_IMAGE` | `ghcr.io/shogogoto/web-timer:latest` | 使用するコンテナイメージ |
| `ADMIN_USERNAME` | `admin` | 初回起動時に作成する管理者名 |
| `ADMIN_PASSWORD` | なし | 初回管理者パスワード。8文字以上の十分に長い値を推奨 |
| `SESSION_SECRET` | なし | Cookieセッション署名用のランダムな秘密値 |
| `COOKIE_SECURE` | `false` | HTTPS運用時は`true` |
| `ALLOW_SHORT_TIMERS` | `false` | 1秒・5秒のデバッグ用タイマーを表示 |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | Web Push管理者の連絡先。`mailto:owner@example.com`形式 |

`ADMIN_USERNAME`と`ADMIN_PASSWORD`は、空のDBへ最初の管理者を作るときだけ使われます。既存DBがある状態で`.env`を書き換えても、既存アカウントの認証情報は変わりません。

`VAPID_SUBJECT`はWeb Pushの署名に必要です。メールアドレスを指定する場合は、次のように`mailto:`を付けます。単なるメールアドレスもアプリ側で補完しますが、設定ファイルでは完全な形式を推奨します。

```dotenv
VAPID_SUBJECT=mailto:owner@example.com
```

`mailto:`または`https://`のどちらでもない値を指定すると、設定ミスが分かるようにアプリは起動時にエラーを出します。

## HTTPSと公開時の注意

Web Pushと通常のブラウザ通知は、`localhost`を除いてHTTPSが必要です。Cloudflare Tunnel、Tailscale Serve、Caddy、NginxなどのHTTPSリバースプロキシからコンテナの8000番へ転送してください。

HTTPSで運用するときは次を設定します。

```dotenv
COOKIE_SECURE=true
```

- インターネットへ8000番を直接公開せず、HTTPSリバースプロキシの背後へ置いてください。
- `.env`、`data/timer.db`、`data/vapid_private.pem`をGitへコミットしないでください。
- 強い管理者パスワードと十分に長い`SESSION_SECRET`を使用してください。
- FirefoxやOSの設定によって、バックグラウンド通知は表示されても通知音が鳴らない場合があります。タブが開いていればアプリ内の終了音も使用されます。

## データとバックアップ

永続データはホストの`./data`に保存されます。

- `data/timer.db`: 利用者とタイマー記録
- `data/vapid_private.pem`: Web Push用秘密鍵

整合性のあるバックアップを取るには、コンテナを一度停止して`data`ディレクトリ全体をコピーします。

```bash
docker compose stop
cp -a data "data.backup-$(date +%Y%m%d-%H%M%S)"
docker compose start
```

復元時はコンテナを停止し、現在の`data`を別の場所へ退避してからバックアップを戻してください。VAPID秘密鍵を失うと既存のWeb Push購読は使えなくなり、利用者側で通知の再登録が必要になります。

## デバッグ用タイマー

終了通知などを確認するときは次を設定すると、1秒と5秒のプリセットが表示されます。通常運用では`false`に戻してください。

```dotenv
ALLOW_SHORT_TIMERS=true
```

## CIとコンテナの公開

GitHub Actionsは次の2本です。

- `CI`: `main`へのpushとPull RequestでテストとDockerビルドを実行
- `Publish container`: `v1.2.3`形式のGitタグでGHCRへ`1.2.3`、`1.2`、`latest`、コミットSHAタグを公開

リリース例:

```bash
git tag v0.1.0
git push origin v0.1.0
```

初回公開後、GitHubのPackages画面でコンテナパッケージの公開範囲を`Public`にしてください。公開処理にはGitHub Actionsが自動発行する`GITHUB_TOKEN`を使うため、Docker Hub用の秘密情報は不要です。

## ライセンス

このソフトウェアは[MIT License](LICENSE)で公開しています。

スクリーンショットは公開用画像を用意した段階でREADMEへ追加する予定です。
