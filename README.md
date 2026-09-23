# podcast-elbarto

Independent X bookmark to private podcast production pipeline. X articles and long posts are snapshotted into a private task queue, adapted and captured with Pi, reviewed, and published through a token protected RSS feed.

## Local setup

```sh
python -m pip install -r requirements-podcast.txt
python -m playwright install chromium
npm ci --prefix podcast
npx --prefix podcast playwright install chromium
```

Place your X Playwright storage state in the ignored `x_session.json`, then run `python podcast_bookmarks.py --count 15` to queue eligible bookmarks. Run `python podcast_cloud.py probe` to test Pi access. Publishing and cloud operation require private credentials and review gates; see [operations](docs/podcast-operations.md).

## Verification

```sh
python -m unittest discover -s tests -p 'test_podcast*.py' -b
npm test --prefix podcast
```

Private article text, sessions, audio, queue state, and credentials must stay outside Git. The GitHub Actions workflows are gated by repository variables; copying them does not enable scheduled production.
