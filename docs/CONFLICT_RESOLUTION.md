# Resolving merge conflicts in this repository

When a merge/rebase stops because of conflicts, follow these steps to pick the right code and keep the app running:

1. **Inspect the conflicted file**
   - Run `git status` to see which files are conflicted.
   - Open the file around the conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) to understand both versions.

2. **Decide which side to keep**
   - *Accept current change* keeps the version already in your branch.
   - *Accept incoming change* keeps the version coming from the branch you are merging/rebasing.
   - *Accept both changes* keeps both blocks; useful only if they are logically compatible.

3. **Example from `main.py` (XRPL flow history)**
   - Keep the block that builds `exchange_xrp` and `ripple_corp_xrp` using the `zero_series` helper so missing columns do not cause `AttributeError`:
     ```python
     zero_series = lambda key: (
         flow_hist_df[key].fillna(0.0)
         if key in flow_hist_df
         else pd.Series(0.0, index=flow_hist_df.index, dtype="float64")
     )

     flow_hist_df["exchange_xrp"] = zero_series("exchange_xrp") / 1e6
     flow_hist_df["ripple_corp_xrp"] = zero_series("ripple_corp_xrp") / 1e6
     ```
   - Choose "accept incoming change" if the incoming branch has this safer helper; otherwise manually apply it and delete the conflict markers.

4. **Clean up and verify**
   - Remove all conflict markers, then run `git add <file>` for each resolved file.
   - Execute `pytest -q` (or the relevant command you use) to ensure the app still works.
   - Finish with `git commit` and re-run your merge/rebase if needed.

This approach keeps the Streamlit dashboard stable by defaulting missing XRPL fields to zeroed series instead of scalars.
