name: Yamato river Fujii water level check

on:
  schedule:
    # 日本時間 7:00〜17:59 の間だけ、30分おきに実行する
    # (cronはUTC基準。UTC 22:00-23:59 と 00:00-08:59 が JST 7:00-17:59 に相当)
    - cron: "*/10 22-23 * * *"
    - cron: "*/10 0-8 * * *"
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Check water level
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          THRESHOLDS: "0.8,0.9,1.0,1.1"
        run: python check_water_level.py

      - name: Commit updated state
        run: |
          git config user.name "river-watch-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data/state.json
          git diff --cached --quiet || git commit -m "update state [skip ci]"
          git push
