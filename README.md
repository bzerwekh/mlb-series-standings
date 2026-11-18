# MLB Series Standings

Standings where each series is one result, decided by total runs across its games.

Live page: https://bzerwekh.github.io/mlb-series-standings/

The page is rebuilt and published by `.github/workflows/pages.yml` on every push to
`main`, or on demand from the Actions tab (Run workflow, with a season input).

```
python3 build.py              # build from cached data in data/
python3 build.py --refresh    # re-download from statsapi.mlb.com first
python3 build.py --season 2027
```

Output: `standings.html` (self-contained; open it directly in a browser).

Files:
- `build.py`: fetches the schedule with line scores, groups games into series, scores them, writes the page.
- `template.html`: page layout and rendering; `build.py` injects the data at `__DATA__`.
- `data/`: cached API responses.

Rules are listed on the page's Rules tab.
