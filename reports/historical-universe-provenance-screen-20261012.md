# Historical USD-M universe provenance screen

The official archive can verify that a dated price or trade file exists, but it does not provide
a dated USD-M `exchangeInfo`/contract-status snapshot. Binance Developer Community explicitly
states that those historical snapshots are not available. A third-party availability index derived
from the archive therefore remains a **descriptive availability proxy**, not proof that a contract
was eligible at the decision time.

Result: the current archive remains diagnostic-only. Alpha 015 and any broad cross-sectional
candidate cannot use a present-day symbol list or archive-file existence as a substitute for
point-in-time eligibility. The permitted route is prospective append-only capture or a separately
audited dated-status source.

Machine-readable record: `research/historical-universe-provenance-screen-20261012.json`.
