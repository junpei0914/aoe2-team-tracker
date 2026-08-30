# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## これは何か

Age of Empires II のチーム戦を固定メンバーで記録・分析するための、ビルド不要の単一HTMLアプリです。Eloレーティング、試合履歴、AIによるチーム分け提案、プレイヤー同士の相性・直接対決成績を扱います。データ・ロジック・UIはすべて [index.html](index.html) の中に収まっています。`package.json`・ビルドツール・リンター・テストは存在しません。

## 実行方法

`index.html` をブラウザで直接開くか、静的サーバーで配信するだけです。

```bash
python -m http.server 8000
```

install/build/lint/test に相当するコマンドはこのリポジトリには存在しません。

## アーキテクチャ

### 実行の仕組み(編集時に重要)

このアプリはバンドラーや開発サーバーを使いません。`<head>` 内で React 18 と Babel Standalone を `unpkg.com` から `<script>` タグで読み込んでいます。実際のアプリコードは `<script type="text/plain" id="app-source">` というブロックの中に置かれています(`type="text/plain"` なのでブラウザはこれをそのまま実行しません)。ページ末尾の別の `<script>` がこのブロックの `textContent` を取り出し、ページ読み込み時に `Babel.transform` でJSX→JSに変換し、その結果を `eval` してReactルートをマウントします。**アプリのロジックを編集する際は、通常の `<script>` タグではなく `#app-source` 内のJSX/JSテキストを編集してください。** また、変換・評価に失敗した場合はキャッチされて `#root` に「起動エラー」として表示される仕組みなので、実際にページを読み込むまで構文エラーをチェックしてくれるリンター等は存在しない点に注意してください。

### データフロー

1. **シードデータ**(`#app-source` 冒頭):`SEED_PLAYERS`、`SEED_MATCHES`、`SEED_TEAMMATE`、`SEED_H2H`、`CHATGPT_RATING` — 過去約221戦分の実績と、手動での「各プレイヤーの強さ」評価をハードコードしたJSONです。これは毎回の再計算の出発点であり、気軽に編集すべきものではありません。
2. **ユーザーが記録した試合**は `loadMatches()` / `saveMatches()` を通じて読み書きされ、Google Apps Script のWebアプリエンドポイント(`#app-source` 下部の `GAS_URL`)経由でGoogleスプレッドシートにJSONをPOST/GETします。これにより無料でデータを端末・訪問間で永続化できています。`GAS_URL` が未設定の場合は `window.storage`(Claude Artifactストレージ、Pro以上が必要)にフォールバックします。
3. **`computeAggregates(userMatches)`** が派生状態の唯一の情報源です。レンダリングのたびに `SEED_MATCHES` + ユーザーの試合を最初からEloアップデート(K=22、`BASE_ELO`=1500)にかけ直し、現在の `players`(elo/games/wins/losses/scoreHistory)、`teammate`(ペアごとの勝率)、`h2h`(直接対決成績)を算出します。Eloを差分更新・保存する仕組みはなく、毎回すべての試合ログから再計算しています。
4. **AIプレイヤー**(`AI1`〜`AI5`)は過去の試合を書き起こした際のラベルの違いに過ぎず、実力差を表すものではありません。そのため全AIは合算勝率から算出した1つの固定Elo(`AI_FIXED_ELO`)を共有し、個々のAIのEloは試合結果によって変動しません(`initialElo` および `computeAggregates` 内の `if (!players[n].isAI)` のガードを参照)。
5. 既知の人間プレイヤーの**初期Elo**は、`CHATGPT_RATING`(外部で一度だけ行った「相対的な強さ」評価)があればそれを優先し、なければ過去の勝率を使い、試合数が少ないプレイヤーほど1500に寄せる縮小(shrinkage)処理をかけます。

### チーム分け提案(`TeamBuilder` / `suggestSplits`)

選択された(偶数人数の)メンバーについて、2チームへの分け方を総当たりで列挙し、各案を `平均Elo + 1.5 * 相性スコア` で評価します。相性スコアは、既知のペア(6戦以上)が強い勝率でチームメイトを組んだ実績があれば加点し、相性の悪い組み合わせを同じチームに固めると減点します。そのうえで、2チーム間の実力差が最も小さい上位5案を返します。

### スコアボードOCR(`AddMatch`)

ユーザーはAoE2の試合終了後スコアボードのスクリーンショットをアップロード/貼り付けできます。base64エンコードして同じApps Scriptエンドポイントに(`type: "ocr_extract"` として)POSTし、Googleドライブの無料OCRにかけてテキストを取得します。`parseScoresFromOcrText` はテキストを1行ずつ走査し、`matchToRosterName`(完全一致→部分一致→レーベンシュタイン距離によるあいまい一致の順)でOCRの誤読があってもプレイヤー名を認識し、続く数値行を最大5個(軍事/経済/テクノロジー/社会/総合)まで貪欲に拾います。これは常に下書き扱いであり、UI側で保存前に必ずレビュー・修正することを求めています。また、チーム分けや勝者をOCRから自動判定することはありません。

## このリポジトリで見られる規約

- UI文言・コードコメント・コミットメッセージはすべて日本語です。
- コミットメッセージは短い命令調の要約(例:`スコア詳細表示改善`、`画像読み取り機能修正`)で、Conventional Commits のようなプレフィックスは使っていません。
- 色・タイポグラフィは `#app-source` 下部の `h2Style`、`cardStyle` などJSのスタイルオブジェクトとしてインラインで定義されています。CSSクラスやスタイルシートを新設するのではなく、このパターンに合わせてください。
