# Moodle課題の草案作成

Moodleの授業ページから課題を検出し、課題文と講義資料を集め、Codex CLIで草案を作ります。標準の出力は `main.tex`。課題文や講義資料にWordの指定がある場合は `main.docx` を作ります。対象サイトの初期設定は `https://lms.omu.moe` です。

## 初回セットアップ

Python 3.11以上、[uv](https://docs.astral.sh/uv/)、[Codex CLI](https://developers.openai.com/codex/cli/) が必要です。

```sh
uv sync
uv run playwright install chromium
codex login
uv run moodle-auto doctor
uv run moodle-auto login
```

`login` は専用ブラウザを開きます。Moodleの通常のログイン操作（SSO・多要素認証を含む）を行うと、ログイン完了を検出してセッションを保存します。普段使っているChromeのプロファイルには触れません。パスワードの入力・保存をこのプログラムに任せる必要はありません。

Moodleのセッションは `.moodle/session.json` に保存します。ディレクトリは所有者のみアクセス可能、セッションファイルは所有者のみ読み書き可能です。セッションが切れたら、もう一度 `login` を実行してください。

GPT側は **Codex CLIのChatGPTログイン（OAuth）** を使います。OAuthトークンを独自に取得してAPIへ転用する実装ではありません。アプリからの実行には `forced_login_method="chatgpt"` を指定し、APIキー環境変数は渡しません。認証とトークン更新はCodexに任せます。利用可否と上限は契約・ワークスペース設定に従います。[OpenAI公式の認証説明](https://developers.openai.com/codex/auth)

## 使い方

```sh
# 課題・期限・提出状況の一覧。GPTは呼び出さない
uv run moodle-auto scan

# 未提出かつ期限前の課題を処理
uv run moodle-auto run

# 特定の課題だけ処理（IDはscanで確認）
uv run moodle-auto run --assignment 37095

# 提出済み・期限切れの課題も、明示指定した場合に処理
uv run moodle-auto run --assignment 37095 --include-submitted
uv run moodle-auto run --assignment 30840 --include-overdue

# 同じ入力でも新しい版を生成。以前の版は保持
uv run moodle-auto run --assignment 37095 --include-submitted --regenerate

# 別の設定ファイルを使用（--configはサブコマンドより前）
uv run moodle-auto --config /absolute/path/moodle.toml scan
```

提出状態が不明な課題は、誤って処理しないよう `needs_review` として残します。提出状態はMoodle本体の課題ページで確認し、Moodle Plusの表示には依存しません。

## 出力

実行中はターミナルにプログレスバーを表示します。授業・セクション・課題の完了件数、現在の資料名、文字抽出の状態、各処理の経過時間を確認できます。課題の処理件数には、提出済みなどの理由でスキップした課題も含みます。

資料はリンクをたどる途中で追加されるため、処理件数を表示します。GPTの応答待ちとログイン待ちはスピナーと経過時間を表示し、推測の完了率は出しません。出力をファイルへリダイレクトした場合は、開始・終了と15秒ごとの進捗ログに切り替わります。

```text
output/<授業ID>/<課題ID>/
  materials/                   ダウンロードした資料
  <入力内容のハッシュ>/
    main.tex                   草案。形式指定によりmain.docx / main.md / main.txt
    assignment.json            課題文・期限・提出状況
    sources.json               資料のURL・抽出本文・ページ番号・保存先
    context.json               GPTに渡した課題と資料
    draft.json                 GPTの構造化された応答
    REVIEW.md                  不足情報・形式指定の根拠・確認事項
    result.json                処理結果
.moodle/
  session.json                 Moodleログイン情報（共有しない）
  catalog.json                 最後に検出した課題一覧・検出失敗の一覧
  last-run.json                最後の生成実行の結果
```

課題や資料が変わると新しい版を作ります。入力が同じならGPTでの生成を省略しますが、更新検出のためMoodleと資料は再取得します。手で編集した草案は上書きしません。生成失敗は完了扱いにせず、再実行時に再試行します。一部の課題だけ失敗しても、別の課題は続行します。多重起動はロックで防ぎます。

## 設定

学籍番号・氏名は環境変数で指定できます。

```sh
export MOODLE_STUDENT_ID='BGA00000'
export MOODLE_STUDENT_NAME='山田 太郎'
uv run moodle-auto run --assignment 27969
```

設定した値をGPTに渡し、草案の著者欄へ反映するよう指示します。未設定・空白の項目はTODOのままです。匿名提出が指定されている場合は記載しません。値を変更すると生成入力が変わるため、新しい版を作ります。既存のTeX/PDFへ後から自動挿入はしません。

`moodle.toml` と同じディレクトリの `.env` からも、この2項目を自動で読み込みます。

```dotenv
MOODLE_STUDENT_ID="BGA00000"
MOODLE_STUDENT_NAME="山田 太郎"
```

`.env` を保存したら、`uv run moodle-auto run --assignment 27969` で実行できます。ターミナルで設定済みの環境変数を優先します。以前の `export` を解除して `.env` に切り替える場合は `unset MOODLE_STUDENT_ID MOODLE_STUDENT_NAME` を実行してください。`--config` を指定した場合も、その設定ファイルと同じディレクトリの `.env` を読みます。

この2項目は生成用の `context.json` にも保存されます。`.env` はGitの管理対象から除外済みです。`.env.example` は空欄のひな形です。他の変数の取り込みや `${...}` の展開は行いません。

`moodle.toml` の相対パスは、設定ファイルのあるディレクトリを基準に解決します。

- `course_ids = []`: ホームページに並ぶ授業を自動検出します。授業がホームに出ない・ページ分割されるサイトでは、対象IDを明示してください。例: `[7129, 7122]`。
- `model = ""`: Codex標準のモデルを使用します。特定モデルを使う場合はアカウントで利用できるIDを指定してください。ユーザーのCodex設定は読み込まないため、モデル指定はこの項目で行います。
- `timezone = "Asia/Tokyo"`: Moodleに表示される日時のタイムゾーン。
- `language = "日本語"`: 草案の既定言語。課題で言語が指定されていれば、その指示を優先します。
- `max_file_mb = 50`: ファイルあたりの取得上限。
- `max_context_chars = 180000`: GPTへ渡す課題・資料本文の文字数予算。入りきらない資料は黙って切り捨てず、確認事項に記録します。
- `codex_timeout_seconds = 900`: 草案生成のタイムアウト。

## 資料と生成の範囲

課題の添付資料と同じ授業内のファイル・フォルダ・ページ・ブックを取得します。同じ授業回の資料を優先してGPTに渡し、前の講義で説明された定義なども参照できるようにします。講義PDF内にしかない設問も、課題番号・授業回と照合するよう指示します。Moodleのコースをセクション別ページに分けている場合も、セクションへのリンクをたどります。

PDF・DOCX・PPTX・UTF-8のTXT/MD/TeX/CSVから文字を抽出します。画像だけのPDF、図の内容、動画、外部サイト、旧Office形式やZIP内の資料は自動解析の対象外です。取得失敗・文字抽出失敗・文脈の上限超過は `REVIEW.md` に残します。画像のOCRは未実装です。

Wordは見出し・段落を持つ実際のDOCXとして生成します。TeXはLuaLaTeX向けです。TeX形式では編集用のmain.texを保存した後、latexmkとLuaLaTeXでmain.pdfまで生成します。TeX LiveまたはMacTeXが必要です。コンパイル失敗時もTeXは保持し、compile.logに詳細を残します。MD・TXTの指定にも対応します。ExcelやPowerPointなど未対応の提出形式は `needs_review` として、必要な形式を報告します。誤った形式を完成品として扱いません。

生成物は確認用の草案です。情報不足はTODOや確認事項として残し、実験結果や本人しか知り得ない情報を作らないよう指示しています。出典IDとページ・スライド番号は元資料と照合してください。数式の正しさ、Wordの最終レイアウト、指定テンプレートへの厳密な適合は、このプログラムでは検証しません。

処理はローカルCLIで完結し、課題と抽出した講義本文をCodexへ送ります。Codexには読み取り専用の一時作業領域を使い、シェル・ブラウザ・アプリ連携を無効にします。Moodleのセッションをプロンプトに含めません。Moodleへの提出操作は実装していません。

## 検証

```sh
uv run pytest -q
uv run ruff check src tests
```

テストでは提出状態、期限、資料リンクの追跡、認証の失効、外部へのリダイレクト拒否、ダウンロード上限、出力形式、再試行、重複生成の防止、手編集の保護を確認します。Moodleのテーマ変更や権限による非表示は実サイトでの確認が必要です。

実装の参照: [Playwrightの認証状態保存](https://playwright.dev/python/docs/auth)、[Codexの非対話実行](https://learn.chatgpt.com/docs/developer-commands#codex-exec)。定期実行の登録はまだ行っていません。

保存済みのTeXだけを再コンパイルする場合（GPT・Moodleへの接続不要）:

```sh
uv run moodle-auto compile output/<授業ID>/<課題ID>/<版>/main.tex
```
