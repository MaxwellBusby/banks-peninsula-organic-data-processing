# AllTrails / Wikiloc GPS Processing

Repository for GPS/trail data extraction, preprocessing and map-matching used for the Master's project. Contains extraction scripts, notebooks and data artifacts (large data is stored under `Data/` and is gitignored).

Getting started

- Create a Python virtual environment and install dependencies.

  ```bash
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt
  ```

- Initialize the repo (already done locally). To create a GitHub repository and push:

  ```bash
  # create repo on GitHub (replace USER and REPO)
  gh repo create USER/REPO --public --source=. --remote=origin --push

  # or manually:
  git remote add origin git@github.com:USER/REPO.git
  git branch -M main
  git push -u origin main
  ```

Notes
- Large datasets are in `Data/` and are excluded from version control. Consider storing them in Git LFS or an external storage.
