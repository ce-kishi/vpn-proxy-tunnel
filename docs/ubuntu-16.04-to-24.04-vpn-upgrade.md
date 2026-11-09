# VPNプロキシ経由でUbuntu 16.04から24.04へ更新する手順

作成日: 2026-11-09

Ubuntu 16.04.7 LTS（amd64）を、VPN接続済みのローカル端末とSSHリバース転送を使って、Ubuntu 24.04 LTSへ段階的に更新する手順です。会話で実際に表示された確認画面と、そのときの対応を含めています。

OS更新コマンドは、利用者が**更新対象ホストで手動実行**します。`proxy-tunnel.sh` はプロキシとSSH転送だけを起動し、リモートのAPT・OS更新・再起動は実行しません。

> 利用者の指定により、システム全体のバックアップは作成しない前提です。更新失敗時には、現地での復旧や再インストールが必要になる場合があります。

## 1. 更新経路と通信の構成

LTSを飛ばさず、次の順に更新します。

```text
16.04 (xenial)
  → 18.04 (bionic)
  → 20.04 (focal)
  → 22.04 (jammy)
  → 24.04 (noble)
```

各段階で、パッケージ更新・再起動・SSH再接続・プロキシトンネルの張り直しを行います。[Ubuntu公式の更新経路](https://ubuntu.com/server/docs/how-to/software/upgrade-your-release/)

```text
更新対象ホストのAPT / curl / snapd
  → http://127.0.0.1:18082
  → SSHリバース転送
  → VPN接続済みローカル端末のプロキシ
  → 接続先の443番
```

現在のプロキシの仕様は次のとおりです。

- ホスト名の許可リストは廃止済みです。
- CONNECT先のポートは443番限定です。通常のHTTP GETを中継するプロキシではありません。
- ローカルの待受けとSSHのリモート転送要求は `127.0.0.1` です。SSHサーバー側の設定も公開範囲に影響します。
- APTの取得先は `https://` に統一します。プロキシ自身のURLは `http://127.0.0.1:18082` で正しい設定です。
- リモートホストの他ユーザーも、転送ポートからローカル端末やVPN経由で到達できる443番宛先へ接続可能です。

## 2. 会話で確認した環境と進捗

### 環境

| 項目 | 提示された値 |
|---|---|
| 初期OS | Ubuntu 16.04.7 LTS、x86_64 |
| 初期カーネル | `4.4.0-210-generic` |
| OS領域 | ext4、438GB、使用366GB、空き50GB |
| EFI領域 | FAT、300MB、空き294MB |
| データ領域 | XFS、5.3TB、使用587GB、空き4.7TB |
| GPU | NVIDIA GeForce GTX 1080 Ti ×10 |
| 初期ドライバー | `550.90.07` |
| パッケージ監査 | `sudo dpkg --audit` に出力なし |
| 保留パッケージ | `apt-mark showhold` に出力なし |
| リリース更新設定 | `Prompt=lts`、メタ情報の取得URLはHTTPS |

上記は会話で確認した一台の値です。別の対象ホストで実行する場合は、そのホストの状態を確認してください。本書にディスクデバイスへ直接書き込む操作は含めていません。

### 確認済みの進捗

- Ubuntu標準リポジトリのHTTPS取得が成功しました。
- 18.04用アップグレーダーのダウンロードと、Ubuntu Archiveの鍵による署名検証が成功しました。
- 16.04→18.04の更新計画で、削除93件・新規656件・更新2,061件が表示されました。
- 削除予定93件の一覧を確認しました。主に旧GUI・旧開発ライブラリで、提示された一覧にSSHや起動の主要パッケージの削除は見当たりませんでした。
- NetworkManagerとGRUBの設定確認、Snapの導入、更新後の不要パッケージ407件の削除確認まで到達しました。
- その後、アップグレーダーのSSH接続確認が再び提示されました。ただし、その時点のOSバージョンは報告されていません。

**再起動後の18.04起動確認、20.04・22.04・24.04への到達、GPUの再構築は、この会話では未確認です。** 現在位置は必ず `lsb_release -ds` で確認します。

## 3. 実行前の確認

更新対象ホストで実行します。

```bash
lsb_release -ds
uname -m
uname -r
df -h / /boot /boot/efi
sudo dpkg --audit
apt-mark showhold
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
```

GPUジョブとコンテナは正常終了してから更新します。空き容量は各段階で再確認し、アップグレーダーが表示する必要容量を満たすことを確認します。

## 4. ローカル端末でプロキシトンネルを起動

VPNへ接続し、`vpn-proxy-tunnel` を取得したディレクトリで実行します。`YOUR_SSH_ALIAS` は対象ホストのSSH別名へ置き換えてください。

```bash
TUNNEL_REMOTE_HOST=YOUR_SSH_ALIAS TUNNEL_PROXY_PORT=18082 \
  ./proxy-tunnel.sh
```

このターミナルは開いたままにします。別のターミナルから対象ホストへ接続します。

```bash
ssh YOUR_SSH_ALIAS
```

**以降のOS更新操作は、対象ホストのSSHセッションで実行します。**

## 5. 最初の一度だけ: APTをUbuntu標準のHTTPS取得先へ統一

このブロックはUbuntu 16.04・amd64専用です。すでに実行済みの場合は、現在の設定を確認して次へ進みます。18.04以降でXenialの取得先へ書き戻してはいけません。

Docker・CUDA・PPA・ローカルCUDAリポジトリなどの追加設定を退避して無効化し、現在のリリースであるXenialの標準取得先を設定します。導入済みソフトの削除はこのブロックでは行いません。

```bash
sudo bash <<'SH'
set -euo pipefail

[ "$(dpkg --print-architecture)" = amd64 ] || {
  echo "amd64向けの手順です。" >&2
  exit 1
}
[ "$(lsb_release -cs)" = xenial ] || {
  echo "Ubuntu 16.04上で実行してください。" >&2
  exit 1
}

disabled_dir="/etc/apt/disabled-for-release-upgrade-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$disabled_dir"

for source_file in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  if [ -f "$source_file" ]; then
    mv -- "$source_file" "$disabled_dir/"
  fi
done

cat > /etc/apt/sources.list <<'EOF'
deb https://archive.ubuntu.com/ubuntu xenial main restricted universe multiverse
deb https://archive.ubuntu.com/ubuntu xenial-updates main restricted universe multiverse
deb https://security.ubuntu.com/ubuntu xenial-security main restricted universe multiverse
deb https://archive.ubuntu.com/ubuntu xenial-backports main restricted universe multiverse
EOF

cat > /etc/apt/apt.conf.d/99vpn-proxy <<'EOF'
Acquire::https::Proxy "http://127.0.0.1:18082";
EOF
SH
```

`99vpn-proxy` は再起動後も残ります。以降のAPT通信にも、起動中のプロキシトンネルが必要です。`sources.list` のリリース名の更新は公式アップグレーダーに任せます。

初期ログの問題は次のように整理しました。

| 症状 | 原因と対応 |
|---|---|
| HTTP取得先で `Temporary failure resolving` | HTTPS用プロキシ設定の対象外だったため、取得先をHTTPSへ変更 |
| 旧NVIDIA `libnvidia-container/ubuntu16.04` で404 | 取得先の不在。OS更新中は追加リポジトリを無効化 |
| `apt.kubernetes.io` が利用できない | 旧取得先は廃止済み。無効化し、必要ならOS更新後に対応版を設定 |
| Bazelで `KEYEXPIRED` | 署名鍵の期限切れ。追加リポジトリを再利用する場合に公式鍵を更新 |
| Cloud SDKで `NO_PUBKEY C0BA5CE6DC6315A3` | 公開鍵不足。追加リポジトリを再利用する場合に公式鍵を登録 |

本手順では追加リポジトリを無効化するため、Bazel・Cloud SDKの鍵更新をOS更新の前提にはしません。署名検証を無効化する設定は使用しません。

## 6. 各段階で: 現在のリリースを完全に更新

まず索引だけを更新し、出力を確認します。

```bash
sudo apt-get update
```

`Err:`、`E:`、取得失敗や署名検証失敗の警告が残る場合は、その原因を解消してから進みます。古いAPTでは警告があっても終了コードが0になる場合があるため、終了コードだけで成功と判断しません。

問題がなければ実行します。削除予定が表示された場合は、その内容を確認します。

```bash
sudo apt-get -o APT::Get::Always-Include-Phased-Updates=true dist-upgrade

sudo apt-get install \
  update-manager-core ubuntu-keyring apt-transport-https \
  ca-certificates curl gnupg gpgv screen

sudo dpkg --audit
apt-mark showhold

if [ -e /run/reboot-required ]; then
  echo "再起動が必要です"
fi
```

監査エラーや更新を妨げる保留があれば、先に解消します。再起動が必要な場合は実行します。

```bash
sudo reboot
```

再起動後、ローカル端末でトンネルを起動し直し、対象ホストへSSH再接続します。

## 7. 次のLTSへ更新するスクリプトを作成

対象ホストで一度だけ作成します。現在のリリースに応じて次のLTSを選び、HTTPSで公式アップグレーダーと署名をダウンロードします。署名検証に成功した場合だけ展開・起動します。

この版には、会話中に発生したGhosttyの端末定義エラーへの対応も含めています。すでに作成済みの旧版を更新する場合も、実行中のアップグレーダーがない状態で行います。

```bash
cat > "$HOME/upgrade-next-lts.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

case "$(lsb_release -cs)" in
  xenial) target=bionic ;;
  bionic) target=focal ;;
  focal)  target=jammy ;;
  jammy)  target=noble ;;
  noble)  echo "Ubuntu 24.04に到達済みです。"; exit 0 ;;
  *) echo "この手順の対象外のリリースです。" >&2; exit 1 ;;
esac

if [[ -e /run/reboot-required ]]; then
  echo "先に再起動し、トンネルを張り直してください。" >&2
  exit 1
fi

if [[ "${TERM:-}" == xterm-ghostty ]]; then
  export TERM=xterm-256color
fi

proxy_url=http://127.0.0.1:18082
stage_dir="$(mktemp -d "/var/tmp/ubuntu-upgrade-${target}-XXXXXX")"
cd "$stage_dir"

base="https://archive.ubuntu.com/ubuntu/dists/${target}-updates/main/dist-upgrader-all/current"

curl -fSL --proxy "$proxy_url" \
  "$base/$target.tar.gz" -o "$target.tar.gz"

curl -fSL --proxy "$proxy_url" \
  "$base/$target.tar.gz.gpg" -o "$target.tar.gz.gpg"

gpgv --keyring /usr/share/keyrings/ubuntu-archive-keyring.gpg \
  "$target.tar.gz.gpg" "$target.tar.gz"

tar -xzf "$target.tar.gz"

sudo env -u http_proxy -u HTTP_PROXY -u no_proxy -u NO_PROXY \
  https_proxy="$proxy_url" HTTPS_PROXY="$proxy_url" \
  "./$target" --frontend=DistUpgradeViewText --mode=server
SH
```

実行します。

```bash
bash "$HOME/upgrade-next-lts.sh"
```

4リリース分のアップグレーダー・署名のHTTPS配布先と、テキスト画面・実行モードの起動オプションは資料作成前に確認しています。各ホストでの更新成功を保証するものではなく、表示される更新計画と実行結果の確認が必要です。

## 8. 実際に表示された確認画面への対応

| 表示・対象 | 会話での対応 | 判断の理由・条件 |
|---|---|---|
| `Continue running under SSH?` | `y` → Enter | SSH経由で更新を続行する選択。追加sshdが起動する |
| `Starting additional sshd`、`press [ENTER]` | Enter | 予備のSSH接続口1022番の案内 |
| `Connection to Snap Store failed` | Snapのプロキシを設定して再確認。下記参照 | APTとは別にsnapdの通信設定が必要 |
| 更新開始の `Continue [yN] Details [d]` | まず `d`。削除一覧を確認後に `y` | 今回は93件全体を確認して続行。他の段階でも削除内訳を確認する |
| `/etc/NetworkManager/NetworkManager.conf` | `N` → Enter | 既存のネットワーク管理設定を維持する選択 |
| `/etc/default/grub` | `keep the local version currently installed` | 既存の起動オプションを維持。GRUBパッケージの更新は続行 |
| 更新後の `Remove obsolete packages?` | 今回は `N` → Enter | 407件の追加削除を保留し、先に更新完了と再起動を確認 |
| 更新完了後の再起動要求 | 更新処理が正常終了していれば `y` | 再起動後にトンネルとSSHを接続し直す |

### 更新計画の詳細表示

`d` で詳細を開き、`Remove:` と `Remove (was auto installed)` の両方を確認します。ページ表示の場合はスペースで進み、`Install:` が始まる直前まで確認します。通常は `q` で確認画面へ戻れます。

今回の93件は、通常の削除7件と、自動インストール扱いの削除86件でした。途中のページまででは56件しか確認できなかったため、続きの37件も確認しました。旧Boost・OpenCV・MPIなどに依存するアプリには、更新後の再ビルドや再設定が必要な場合があります。

`No longer supported:` はサポート区分の通知で、その欄自体が削除予定を示すものではありません。

### GRUBの選択画面

↑↓で `keep the local version currently installed` を選び、Tabで `<Ok>` に移動してEnterを押します。その他の設定ファイル確認も、対象と差分を確認して選びます。すべての確認画面で一律に同じ回答をする手順ではありません。

### 予備SSHの1022番

1022番は更新中の予備SSH、18082番はプロキシ用です。用途は異なります。

```bash
ssh -p 1022 YOUR_SSH_ALIAS
```

予備接続には、接続元から1022番への到達性が必要です。アップグレーダーはファイアウォールを自動変更しません。今回、ファイアウォールを変更した記録はありません。画面に示される全接続元向けのiptables開放コマンドを、この資料では自動実行しません。

## 9. 発生したエラーと対処

### Ghostty: `Cannot find terminfo entry for 'xterm-ghostty'`

対象ホストに端末定義がないことが原因です。会話では次の対処を案内しました。

```bash
export TERM=xterm-256color
bash "$HOME/upgrade-next-lts.sh"
```

この環境変数の変更は現在のSSHセッションに適用されます。本書のスクリプトには `xterm-ghostty` の場合の切り替えを組み込んでいます。[Ghostty公式説明](https://ghostty.org/docs/features/ssh)

### Snap Storeへの接続失敗

APTのプロキシ設定や、アップグレーダーのシェルの環境変数だけでは、常駐するsnapdの通信設定を満たせない場合があります。

接続失敗の確認画面では `n` で一度中止し、対象ホストで設定・確認します。

```bash
sudo snap set system proxy.https="http://127.0.0.1:18082"
sudo snap debug connectivity
```

接続確認が `PASS` になったら、アップグレーダーを再実行します。失敗が残る場合は、その出力で切り分けます。[Snapメンテナーによるプロキシ設定の説明](https://forum.snapcraft.io/t/preferred-method-for-configuring-proxy-settings-for-snapd/11743)

後続のログでは `gtk-common-themes`、`gnome-calculator`、`core18`、`gnome-characters`、`gnome-logs`、`gnome-system-monitor` の導入メッセージを確認しました。

### AppStreamのGLib警告

```text
GLib-CRITICAL: g_strchomp: assertion 'string != NULL' failed
AppStream cache update completed, but some metadata was ignored due to errors.
```

今回のログでは、この警告後もパッケージ更新の計算と開始確認まで進みました。この表示だけを理由に更新失敗とは判定しません。APT・dpkgの処理エラーが別途出ている場合は、そのエラーを確認します。

### 署名検証失敗・通信失敗

| 表示 | 確認事項 |
|---|---|
| `gpgv: Good signature` | 署名検証成功。会話ではUbuntu Archive Automatic Signing Key (2012)による検証成功を確認 |
| `NO_PUBKEY`、`BAD signature`、署名検証失敗 | 展開・実行を続行しない。取得元、`ubuntu-keyring`、取得ファイルを確認 |
| `Connection refused` | VPN、トンネル、対象ホスト、18082番、再起動後の張り直しを確認 |
| `403 Forbidden` | CONNECT先が443番か確認。現在はホスト名制限なし |
| `405 Method Not Allowed` | HTTPの通常リクエストをHTTPS専用プロキシへ送っていないか確認 |
| HTTPの取得先で名前解決失敗 | APTの取得先がHTTPSであるか確認 |

## 10. 各段階の終了後: 再起動して確認

更新が正常終了したら、アップグレーダーの案内に従って再起動します。再起動でSSHとプロキシトンネルは切れるため、再度接続します。

対象ホストで実行します。

```bash
lsb_release -ds
uname -r
sudo dpkg --audit
apt-mark showhold
df -h /
```

SSH、ネットワーク、必要なデータ領域のマウントも確認します。GPUドライバーは途中のカーネル更新で再構築や再導入が必要になる場合があります。

現在のリリースが確認できたら、第6節の現行リリース更新を行い、問題がなければ次のLTSへ進みます。

| 現在 | 次にスクリプトが選ぶ更新先 |
|---|---|
| 16.04 / xenial | 18.04 / bionic |
| 18.04 / bionic | 20.04 / focal |
| 20.04 / focal | 22.04 / jammy |
| 22.04 / jammy | 24.04 / noble |
| 24.04 / noble | 更新せず終了 |

正常終了する前に、別のAPT・dpkg・アップグレーダーを重ねて起動しません。不要パッケージの整理は、起動確認後に削除候補を見て行います。

### SSHが切れた場合

VPNとトンネルを再接続し、対象ホストへログインします。更新処理がscreen内で継続していないか確認します。

```bash
export TERM=xterm-256color
sudo screen -ls
```

更新セッションが残っている場合は、そのIDを指定して再接続します。`SESSION_ID` は一覧に表示された実際のIDへ置き換えます。

```bash
sudo screen -D -r SESSION_ID
```

## 11. Ubuntu 24.04到達後のGPU環境

利用者から、既存のCUDA環境は24.04対応版へ入れ替えてよいと指定されています。

同じGTX 1080 Ti（Pascal）を使う場合、580系プロプライエタリドライバーとCUDA 12.9以前を基本に、使用アプリの要件を合わせます。Pascal向けのCUDAオフラインコンパイル対応は12.9までで、Open版GPUカーネルモジュールもPascalを対象としていません。

- [NVIDIA: GPUアーキテクチャの対応方針](https://developer.nvidia.com/blog/navigating-gpu-architecture-support-a-guide-for-nvidia-cuda-developers/)
- [NVIDIA: カーネルモジュールの対応範囲](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/610/kernel-modules.html)
- [NVIDIA: CUDA 12.9のLinux導入資料](https://docs.nvidia.com/cuda/archive/12.9.1/cuda-installation-guide-linux/index.html)

Ubuntu 16.04向けのCUDA・NVIDIA・Docker・PPA設定をそのまま戻さず、24.04と使用GPUに対応する取得先・バージョンを設定します。GPU環境の再構築と検証は、今回の会話では未実施です。

## 12. 到達点を記録するチェック欄

会話で確認できていないため、以下は未チェックです。実機で確認できた項目だけを記録します。

- [ ] 再起動後にUbuntu 18.04で起動し、SSH・通信・`dpkg --audit` を確認
- [ ] 再起動後にUbuntu 20.04で起動し、同様に確認
- [ ] 再起動後にUbuntu 22.04で起動し、同様に確認
- [ ] 再起動後にUbuntu 24.04で起動し、同様に確認
- [ ] 必要なデータ領域とアプリケーションを確認
- [ ] GPUドライバーを導入し、`nvidia-smi` で必要なGPUを確認
- [ ] CUDA・利用フレームワークで実際のGPU計算を確認

## 参考資料

- [Ubuntu: リリースアップグレード](https://ubuntu.com/server/docs/how-to/software/upgrade-your-release/)
- [Ubuntu: LTSアップグレードの配布情報](https://changelogs.ubuntu.com/meta-release-lts)
- [Ubuntu: bionic-updatesの公式アップグレーダー配布先](https://archive.ubuntu.com/ubuntu/dists/bionic-updates/main/dist-upgrader-all/current/)
- [Kubernetes: 旧パッケージリポジトリの廃止](https://kubernetes.io/blog/2023/08/31/legacy-package-repository-deprecation/)
- [Bazel: Ubuntu向け公式導入手順・署名鍵](https://bazel.build/install/ubuntu)
- [Google Cloud CLI: 公式導入手順・署名鍵](https://docs.cloud.google.com/sdk/docs/install-sdk)
