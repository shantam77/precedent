## Cold start (HEAD, 0 LLM tokens)

| repo | files | components | libraries | decisions | enforced | observed only | people | seconds |
|---|---|---|---|---|---|---|---|---|
| netbox | 2,297 | 27 | 152 | 45 | 23 | 21 | 459 | 11.3 |
| mitmproxy | 1,295 | 25 | 220 | 118 | 62 | 47 | 602 | 20.3 |
| paperless-ngx | 1,566 | 30 | 271 | 104 | 46 | 57 | 440 | 14.0 |
| wagtail | 5,243 | 34 | 241 | 103 | 48 | 44 | 1004 | 18.0 |
| outline | 2,860 | 51 | 502 | 249 | 127 | 119 | 250 | 18.3 |
| saleor | 4,724 | 72 | 427 | 326 | 201 | 97 | 297 | 38.2 |
| sentry | 21,739 | 192 | 952 | 560 | 342 | 195 | 956 | 480.3 |

## History replay (graph as of the cut, every later real commit checked)

| repo | cut | commits | false alarms | asks on real reversals | decisions caught (resurrected) | edits caught | decisions still true at HEAD | check p50 | files with context | tokens when shown |
|---|---|---|---|---|---|---|---|---|---|---|
| netbox | 2024-01-01 | 3,579 | 0 | 1 | 13/13 | 43/56 | 31/37 | 8.41 ms | 128/1500 | 60 |
| mitmproxy | 2024-01-01 | 779 | 0 | 4 | 54/55 | 155/202 | 87/90 | 12.52 ms | 477/818 | 52 |
| paperless-ngx | 2024-01-01 | 3,465 | 0 | 0 | 27/27 | 44/86 | 72/72 | 21.65 ms | 454/1420 | 96 |
| wagtail | 2024-01-01 | 4,159 | 0 | 2 | 46/46 | 98/132 | 96/104 | 27.78 ms | 975/1500 | 58 |
| outline | 2024-01-01 | 4,645 | 0 | 0 | 40/40 | 108/199 | 167/169 | 20.66 ms | 978/1500 | 57 |
| saleor | 2024-01-01 | 1,615 | 0 | 0 | 40/40 | 692/753 | 293/293 | 32.87 ms | 899/1500 | 119 |
| sentry | 2025-06-01 | 26,998 | 8 | 3 | 39/40 | 125/164 | 356/362 | 85.52 ms | 890/1500 | 202 |

Total: 45,240 real commits replayed, 8 false-alarm commits.

## Agent A/B (headless Claude Code, Haiku 4.5)

| task | named | reintroduced off | reintroduced on | on: mentions decision | turns off/on | $ off/on |
|---|---|---|---|---|---|---|
| mitmproxy_blinker | True | False | False | True | 11/8 | 0.098/0.077 |
| netbox_cacheops | True | True | False | True | 6/7 | 0.142/0.078 |
| netbox_drfyasg | True | False | False | False | 3/3 | 0.069/0.064 |
| outline_koarouter | True | False | False | False | 21/16 | 0.182/0.120 |
| outline_lodash | False | False | False | False | 9/8 | 0.104/0.091 |
| paperless_bleach | True | True | False | True | 10/8 | 0.080/0.063 |
| paperless_tqdm | False | False | False | False | 4/4 | 0.061/0.056 |
| saleor_html2text | False | False | False | False | 3/3 | 0.034/0.040 |
| saleor_markdown | True | True | False | True | 7/6 | 0.054/0.043 |
| saleor_psycopg2 | True | True | False | True | 5/8 | 0.058/0.068 |
| wagtail_html5lib | True | True | False | True | 4/3 | 0.046/0.047 |
| wagtail_tablib | True | True | False | True | 4/4 | 0.049/0.052 |
