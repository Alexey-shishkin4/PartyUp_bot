# Attribution for the trained game-affinity database

Contains information from **Steam Video Games**, published by the Tamber Team
on Kaggle: <https://www.kaggle.com/datasets/tamber/steam-video-games>.
The source metadata was retrieved on 2026-11-17; last source update: 2017-03-09.
Kaggle lists its license as “Database: Open Database, Contents: Database Contents”.

The source database is made available under the Open Database License (ODbL)
1.0; its contents under the Database Contents License (DbCL) 1.0.

The derived database **app/resources/affinity.json**, containing aggregate item-item
similarities, is distributed under **ODbL 1.0**, with contents under **DbCL 1.0**.
The application code is separately licensed under MIT. The live application
database of consented profiles is stored separately from this aggregate file.

Complete license texts are in [licenses/ODbL-1.0.txt](licenses/ODbL-1.0.txt)
and [licenses/DbCL-1.0.txt](licenses/DbCL-1.0.txt).

The package includes the full machine-readable derivative. The Telegram bot
sends it and the evaluation report with the `/model` command. Attribution and
license links are available in `/model` and `/about`.

Reproduction: download `steam-200k.csv` from the source above and run
`python scripts/train_model.py --csv PATH/steam-200k.csv`.
Exact source SHA-256 and training/evaluation settings are in
`app/resources/model_report.json`; source metadata is in `app/resources/dataset_metadata.json`.

No Kaggle account IDs, account profiles or user-level interactions are included
in the model, synthetic demo profiles, or distributed application database.
Game names and Steam are trademarks of their respective owners; the project
is independent of Valve.
