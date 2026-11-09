# vpn-proxy-tunnel

インターネットへ直接接続できないSSHサーバーから、VPN接続済みのローカル端末を
経由してHTTPS通信を行うためのプロキシとSSHトンネルです。
APTなどのコマンドは、利用者が別のSSHセッションでリモート側に入力します。

Ubuntu 16.04から24.04への段階更新は、[VPNプロキシ経由のリリース更新手順](docs/ubuntu-16.04-to-24.04-vpn-upgrade.md)にまとめています。

## 構成

| ファイル | 役割 |
|---|---|
| `proxy-tunnel.sh` | ローカルプロキシとSSHリバース転送を起動・終了 |
| `select_python3.sh` | 動作するPython 3を選択 |
| `http_connect_proxy.py` | 任意のホストの443番へのCONNECTを中継 |
| `.env.example` | 接続設定のひな形 |

## 必要なもの

- ローカル端末: Bash、Python 3.9以降、OpenSSHクライアント
- ローカル端末からリモートサーバーへのSSH接続（必要に応じてVPN）
- ローカル端末から接続先ホストへのHTTPS接続
- リモートサーバー: SSHサーバーとリバース転送の許可

Pythonの追加ライブラリは不要です。SSHの認証・ホスト鍵確認には通常のOpenSSH設定を使います。

## ローカルでトンネルを起動する

```bash
cp .env.example .env
```

`.env` の接続先を編集します。SSH設定のホスト別名も使用できます。

```dotenv
TUNNEL_REMOTE_HOST=user@example-host
TUNNEL_PROXY_PORT=18082
```

設定を読み込んで起動します。`.env` は自動では読み込みません。

```bash
set -a
source .env
set +a
./proxy-tunnel.sh
```

リモート側の `http://127.0.0.1:18082` が、ローカル端末上のプロキシへ転送されます。
VPNとこのターミナルは利用中ずっと開いたままにしてください。
Ctrl+Cで今回起動したSSH接続とプロキシを終了します。

スクリプトは `ssh -N -T` を使用します。リモートコマンドやsudoを実行せず、
APT取得先・パッケージ・サービスを変更しません。
ローカルとリモートの待受けは `127.0.0.1` 限定です。
いずれかのポートが使用中なら起動に失敗します。
プロキシの待受け完了を確認してからSSH転送を開始します。
CONNECT後は無通信時間だけを理由に切断せず、送信待ちの間も逆方向の応答を中継します。

### 設定項目

| 環境変数 | 内容 | 既定値 |
|---|---|---|
| `TUNNEL_REMOTE_HOST` | SSH接続先（必須） | なし |
| `TUNNEL_PROXY_PORT` | 両端で使用するプロキシポート | `18082` |
| `TUNNEL_PYTHON_BIN` | Python実行ファイルの指定 | PATHと一般的な配置先から選択 |

`.env` には接続先だけを記載し、SSHのパスワードや秘密鍵は記載しないでください。
`.env` はGitの対象外です。

## リモート側でAPTを実行する

別のターミナルから同じサーバーへSSH接続し、リモート側で実行します。
取得先URLはHTTPSである必要があります。接続先ホスト名の制限はありません。

```bash
sudo apt -o Acquire::https::Proxy=http://127.0.0.1:18082 \
  -o APT::Update::Error-Mode=any update &&
sudo apt -o Acquire::https::Proxy=http://127.0.0.1:18082 upgrade
```

ポートを変更した場合はコマンドにも同じ番号を指定してください。
`APT::Update::Error-Mode=any` は、インデックスの取得失敗時に後続の更新へ進むことを防ぎます。

削除を伴う依存関係の入れ替えが必要なパッケージは `upgrade` で保留されることがあります。
まずシミュレーションで変更内容を確認してください。

```bash
apt --simulate full-upgrade
```

削除対象・GPU処理への影響・再起動の必要性を確認したうえで適用する場合:

```bash
sudo apt -o Acquire::https::Proxy=http://127.0.0.1:18082 full-upgrade
```

更新後の確認例です。カーネル更新で再起動が必要な場合は、作業を保存して再起動した後に確認します。

```bash
uname -r
sudo dpkg --audit
# NVIDIA GPU搭載サーバーの場合
nvidia-smi
```

## 接続先と待受け

任意のホストの443番へ接続できます。許可リストの設定は不要です。
ローカルの待受けとSSHのリモート転送要求は `127.0.0.1` に限定しています。
リモートサーバーの他ユーザーも、転送ポートを通じてローカル端末やVPNから
到達できる任意の443番宛先へ接続できます。

プロキシを直接起動する場合:

```bash
python3 http_connect_proxy.py 18082
```

## エラーの切り分け

- `403 Forbidden`: 宛先ポートが443番であるか確認します。
- `Connection refused`: トンネルの端末・VPN・SSH接続・ポート番号を確認します。
- HTTP取得先への直接接続で失敗: 本プロキシはHTTPS CONNECT専用です。取得先をHTTPSへ変更します。
- `dpkg` の設定エラー: 失敗したパッケージの設定ログやサービスログを確認します。
  トンネルの疎通成功だけでは、パッケージ設定の成功は確認できません。

Ubuntu 24.04 arm64で、`ubuntu.sources` の取得先が
`http://ports.ubuntu.com/ubuntu-ports/` になっている場合の変更例です。
対象ファイルを確認したうえで、リモート側で実行します。

```bash
sudo cp -a /etc/apt/sources.list.d/ubuntu.sources \
  "/var/backups/ubuntu.sources.$(date +%Y%m%d-%H%M%S)"
sudo sed -i 's|http://ports\.ubuntu\.com/ubuntu-ports/|https://ports.ubuntu.com/ubuntu-ports/|g' \
  /etc/apt/sources.list.d/ubuntu.sources
grep '^URIs:' /etc/apt/sources.list.d/ubuntu.sources
```

## テスト

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n proxy-tunnel.sh select_python3.sh
```

テストは外部ネットワークを使用せず、任意ホストの許可、443番以外の拒否、ローカルCONNECT通信、
ポート競合、起動遅延時のSSH開始順序、送信詰まり時の逆方向応答、
無通信後の継続、送信終了時のバッファ転送を検証します。
