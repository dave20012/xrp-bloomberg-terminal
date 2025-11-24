# Build Logs for `main`

## xrpl_inflow_monitor
```
2025-11-24T19:04:32.611481450Z [inf]  1:M 24 Nov 2025 19:04:31.162 * Background saving terminated with success
2025-11-24T19:04:32.611537940Z [inf]  1:M 24 Nov 2025 19:04:31.061 * 1 changes in 60 seconds. Saving...
2025-11-24T19:04:32.611546060Z [inf]  1:M 24 Nov 2025 19:04:31.061 * Background saving started by pid 362
2025-11-24T19:04:32.611554610Z [inf]  362:C 24 Nov 2025 19:04:31.064 * BGSAVE done, 15 keys saved, 0 keys skipped, 5593 bytes written.
2025-11-24T19:04:32.611563034Z [inf]  362:C 24 Nov 2025 19:04:31.070 * DB saved on disk
2025-11-24T19:04:32.611583367Z [inf]  362:C 24 Nov 2025 19:04:31.071 * Fork CoW for RDB: current 0 MB, peak 0 MB, average 0 MB
2025-11-24T19:05:42.081193407Z [inf]  363:C 24 Nov 2025 19:05:32.104 * Fork CoW for RDB: current 0 MB, peak 0 MB, average 0 MB
2025-11-24T19:05:42.081207912Z [inf]  1:M 24 Nov 2025 19:05:32.196 * Background saving terminated with success
2025-11-24T19:05:42.081445976Z [inf]  1:M 24 Nov 2025 19:05:32.094 * 1 changes in 60 seconds. Saving...
2025-11-24T19:05:42.081454875Z [inf]  1:M 24 Nov 2025 19:05:32.095 * Background saving started by pid 363
2025-11-24T19:05:42.081466653Z [inf]  363:C 24 Nov 2025 19:05:32.097 * BGSAVE done, 15 keys saved, 0 keys skipped, 5599 bytes written.
2025-11-24T19:05:42.081476419Z [inf]  363:C 24 Nov 2025 19:05:32.103 * DB saved on disk
2025-11-24T19:06:03.340673050Z [err]  WARNING:__main__:GET https://api.coingecko.com/api/v3/coins/ripple/ohlc throttled with 429; falling back to cache when possible
2025-11-24T19:06:03.340678341Z [err]  WARNING:__main__:GET https://api.coingecko.com/api/v3/coins/ripple/market_chart throttled with 429; falling back to cache when possible
2025-11-24T19:06:41.438991377Z [inf]  1:M 24 Nov 2025 19:06:33.018 * 1 changes in 60 seconds. Saving...
2025-11-24T19:06:41.438998373Z [inf]  1:M 24 Nov 2025 19:06:33.018 * Background saving started by pid 385
2025-11-24T19:06:41.439008747Z [inf]  385:C 24 Nov 2025 19:06:33.021 * BGSAVE done, 15 keys saved, 0 keys skipped, 5600 bytes written.
2025-11-24T19:06:41.439035983Z [inf]  385:C 24 Nov 2025 19:06:33.028 * DB saved on disk
2025-11-24T19:06:41.439042619Z [inf]  385:C 24 Nov 2025 19:06:33.029 * Fork CoW for RDB: current 0 MB, peak 0 MB, average 0 MB
2025-11-24T19:06:41.439049479Z [inf]  1:M 24 Nov 2025 19:06:33.119 * Background saving terminated with success
2025-11-24T19:06:52.942179944Z [err]  WARNING:__main__:GET https://api.coingecko.com/api/v3/simple/price throttled with 429; falling back to cache when possible
2025-11-24T19:07:41.702970217Z [inf]  394:C 24 Nov 2025 19:07:34.066 * DB saved on disk
2025-11-24T19:07:41.702973505Z [inf]  1:M 24 Nov 2025 19:07:34.057 * Background saving started by pid 394
2025-11-24T19:07:41.702984527Z [inf]  394:C 24 Nov 2025 19:07:34.059 * BGSAVE done, 15 keys saved, 0 keys skipped, 5597 bytes written.
2025-11-24T19:07:41.702989875Z [inf]  394:C 24 Nov 2025 19:07:34.067 * Fork CoW for RDB: current 0 MB, peak 0 MB, average 0 MB
2025-11-24T19:07:41.703001664Z [inf]  1:M 24 Nov 2025 19:07:34.157 * Background saving terminated with success
2025-11-24T19:07:41.703139007Z [inf]  1:M 24 Nov 2025 19:07:34.056 * 1 changes in 60 seconds. Saving...
2025-11-24T19:07:42.960963776Z [err]  WARNING:__main__:GET https://api.coingecko.com/api/v3/simple/price throttled with 429; falling back to cache when possible
2025-11-24T19:08:41.226766813Z [inf]  1:M 24 Nov 2025 19:08:35.180 * Background saving terminated with success
2025-11-24T19:08:41.226840778Z [inf]  1:M 24 Nov 2025 19:08:35.079 * 1 changes in 60 seconds. Saving...
2025-11-24T19:08:41.226846453Z [inf]  1:M 24 Nov 2025 19:08:35.079 * Background saving started by pid 395
2025-11-24T19:08:41.226852616Z [inf]  395:C 24 Nov 2025 19:08:35.082 * BGSAVE done, 15 keys saved, 0 keys skipped, 5592 bytes written.
2025-11-24T19:08:41.226858761Z [inf]  395:C 24 Nov 2025 19:08:35.090 * DB saved on disk
2025-11-24T19:08:41.226864450Z [inf]  395:C 24 Nov 2025 19:08:35.090 * Fork CoW for RDB: current 0 MB, peak 0 MB, average 0 MB
```

## sentiment-worker
```
2025-11-24 18:48:13,515 | INFO | Valid headlines: 30
2025-11-24 18:48:23,774 | INFO | Pushed → {
  'timestamp': '2025-11-24T18:48:23.772878Z',
  'score': -0.3187477252601335,
  'count': 24,
  'mode': 'weighted_all',
  'articles': [
    {'source': 'Biztoc.com', 'title': "Bitcoin, Ethereum, XRP, Dogecoin Trim Losses Ahead As 'Extreme Fear' Continues", 'pos': 0.10029864311218262, 'neg': 0.8375481963157654, 'neu': 0.06215314939618111, 'scalar': -0.7372495532035828, 'weight': 0.05},
    {'source': 'Slashdot.org', 'title': 'Bitcoin, XRP and Dogecoin Pummeled as Crypto Liquidations Top $2.2 Billion - Decrypt', 'pos': 0.036002304404973984, 'neg': 0.7459684014320374, 'neu': 0.21802930533885956, 'scalar': -0.7099660970270634, 'weight': 0.15},
    {'source': 'Biztoc.com', 'title': 'XRP News Today: ETF Hopes Fade as Reclassification Fears Hit XRP', 'pos': 0.016166795045137405, 'neg': 0.939756453037262, 'neu': 0.04407675936818123, 'scalar': -0.9235896579921246, 'weight': 0.05},
    {'source': 'Bitcoinist', 'title': 'Here’s Why A Supply Shock Could Be Imminent For XRP', 'pos': 0.04284033551812172, 'neg': 0.29366183280944824, 'neu': 0.663497805595398, 'scalar': -0.2508214972913265, 'weight': 0.35},
    {'source': 'Cryptonews', 'title': 'NYSE Approves Listings for Grayscale’s XRP and Dogecoin ETFs', 'pos': 0.16529057919979095, 'neg': 0.00913588609546423, 'neu': 0.8255735039710999, 'scalar': 0.15615469310432673, 'weight': 0.15},
    {'source': 'Biztoc.com', 'title': 'XRP Up 89% in One Year While Bitcoin Gains Just 3.6%: What’s Driving the Gap?', 'pos': 0.12682625651359558, 'neg': 0.07389388233423233, 'neu': 0.7992798089981079, 'scalar': 0.05293237417936325, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'Explained: Why is Bitcoin, XRP down today?', 'pos': 0.02081720530986786, 'neg': 0.6645686626434326, 'neu': 0.3146141469478607, 'scalar': -0.6437514573335648, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'XRP Drops With Market as Bitcoin Weakness Pulls Altcoins Into Oversold Territory', 'pos': 0.016174685209989548, 'neg': 0.9584130644798279, 'neu': 0.025412224233150482, 'scalar': -0.9422383792698383, 'weight': 0.05},
    {'source': 'Ambcrypto.com', 'title': 'Ripple: 2 ETFs are now live on NYSE, yet XRP fell below $2 – Just bad timing?', 'pos': 0.021930256858468056, 'neg': 0.8616672158241272, 'neu': 0.1164025291800499, 'scalar': -0.8397369589656591, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'Bitcoin, XRP and Dogecoin Pummeled as Crypto Liquidations Top $2.2 Billion', 'pos': 0.021188152953982353, 'neg': 0.9185309410095215, 'neu': 0.06028080731630325, 'scalar': -0.8973427880555391, 'weight': 0.05},
    {'source': 'ZyCrypto', 'title': 'Grayscale Poised To Debut XRP And Dogecoin ETFs On Monday Following NYSE Approvals', 'pos': 0.4560626745223999, 'neg': 0.008361362852156162, 'neu': 0.5355759859085083, 'scalar': 0.44770131167024374, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'Why trouble for the biggest foreign buyer of U.S. debt could ripple through America’s bond market', 'pos': 0.028230370953679085, 'neg': 0.8326693773269653, 'neu': 0.13910016417503357, 'scalar': -0.8044390063732862, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'Grayscale’s Dogecoin and XRP ETFs Set for NYSE Debut on November 24', 'pos': 0.06490855664014816, 'neg': 0.010511639527976513, 'neu': 0.9245797991752625, 'scalar': 0.05439691711217165, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'Why fiscal trouble in Japan could ripple through U.S. financial markets', 'pos': 0.021227896213531494, 'neg': 0.772643506526947, 'neu': 0.2061285823583603, 'scalar': -0.7514156103134155, 'weight': 0.05},
    {'source': 'Crypto Briefing', 'title': 'Hyperliquid whale sees profit fall from $100M to $38.4M as ETH and XRP longs sink', 'pos': 0.007073794025927782, 'neg': 0.9755065441131592, 'neu': 0.017419645562767982, 'scalar': -0.9684327500872314, 'weight': 0.15},
    {'source': 'newsBTC', 'title': 'XRP Approaches Macro Breakdown Zone, Analyst Warns About One Final Leg Lower', 'pos': 0.02778635174036026, 'neg': 0.9241708517074585, 'neu': 0.04804285988211632, 'scalar': -0.8963844999670982, 'weight': 0.15},
    {'source': 'newsBTC', 'title': 'XRP ETFs Could See Aggressive Accumulation – Here Are The Numbers', 'pos': 0.09003706276416779, 'neg': 0.03174475207924843, 'neu': 0.8782181739807129, 'scalar': 0.05829231068491936, 'weight': 0.15},
    {'source': 'Biztoc.com', 'title': 'Grayscale XRP and Dogecoin ETFs to debut November 24 following SEC green light', 'pos': 0.09571713209152222, 'neg': 0.009409165009856224, 'neu': 0.8948737382888794, 'scalar': 0.08630796708166599, 'weight': 0.05},
    {'source': 'Biztoc.com', 'title': 'Grayscale’s Dogecoin and XRP ETFs tee up to launch on Monday following NYSE approvals', 'pos': 0.16711971163749695, 'neg': 0.009387869387865067, 'neu': 0.8234923481941223, 'scalar': 0.15773184224963188, 'weight': 0.05},
    {'source': 'Bitcoinist', 'title': 'Market Expert Says Investors Will No Longer Be Able To Buy XRP Directly – Here’s Why', 'pos': 0.03680155798792839, 'neg': 0.07044578343629837, 'neu': 0.8927527070045471, 'scalar': -0.03364422544836998, 'weight': 0.35},
    {'source': 'Biztoc.com', 'title': "Grayscale's DOGE, XRP ETFs to Go Live on NYSE Monday", 'pos': 0.05709664896130562, 'neg': 0.01155601441860199, 'neu': 0.9313473105430603, 'scalar': 0.04554063454270363, 'weight': 0.05},
    {'source': 'Yahoo Entertainment', 'title': 'XRP Is Valued At $130 Billion, But Makes Only $5,000 A Day In Revenue: What Gives?', 'pos': 0.025650979951024055, 'neg': 0.1605595499277115, 'neu': 0.8137894868850708, 'scalar': -0.13490856997668743, 'weight': 0.15},
    {'source': 'Bitcoinist', 'title': 'XRP Capitulation: Investors Now Realizing $75 Million In Loss Every Day', 'pos': 0.014723301865160465, 'neg': 0.9542032480239868, 'neu': 0.031073397025465965, 'scalar': -0.9394799461588264, 'weight': 0.35},
    {'source': 'CryptoSlate', 'title': 'Why XRP holders are suddenly feeling the full force of Bitcoin’s liquidity crunch', 'pos': 0.13918519020080566, 'neg': 0.031274210661649704, 'neu': 0.8295405507087708, 'scalar': 0.10791097953915596, 'weight': 0.35}
  ]
}
```
