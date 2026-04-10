# Solar Energy Analysis - DATA 201

Analyzing solar installation trends, costs, and energy production potential in the Bay Area using data from NREL, NASA POWER, and Berkeley Lab's Tracking the Sun dataset.

## Project Structure

```
notebooks/
  01_nrel_api.ipynb          # NREL PVWatts API exploration
  02_nasa_api.ipynb          # NASA POWER API exploration
  03_data_loading.ipynb      # Download & load Berkeley Lab data
  04_eda_peninsula.ipynb     # Exploratory data analysis (Bay Area)
  05_data_dictionary.ipynb   # Column definitions & data reference
  06_pipeline.ipynb          # End-to-end integration pipeline
docs/                        # Sprint notes, meeting minutes
```

## Data Sources

| Source | Description | Access |
|--------|-------------|--------|
| **NREL PVWatts v8** | Modeled solar production estimates | [API](https://developer.nrel.gov/docs/solar/pvwatts/v8/) (key required) |
| **NASA POWER** | Hourly solar radiation & weather data | [API](https://power.larc.nasa.gov/) (open) |
| **Tracking the Sun** | Real solar installation records (Berkeley Lab) | [Google Drive](https://drive.google.com/file/d/1NQh4TRC_IqDz2r5vfZuxDm6LGjEuexdu/view) |
| **Kaggle Supplement** | Urban solar ROI dataset | [Kaggle](https://www.kaggle.com/datasets/shaistashahid/urban-solar-roi-and-sustainability) |

## Setup

1. Clone the repo
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add your NREL API key
4. Run notebooks in order (01 through 06)

## Agile Workflow

We use **GitHub Issues** and **GitHub Projects** for task management:
- Each notebook module has an assigned owner
- Issues are labeled by sprint and category
- Use the project board to track progress: To Do / In Progress / Done

## Team

| Member | Role |
|--------|------|
| | |
| | |
| | |
| | |
