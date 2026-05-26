import geopandas as gpd
import pandas as pd

edges_path = r'Data/Map_Matching/Graph_Filter_all/edges.shp'
strava_path = r'Data/Strava/strava_2022_bankspeninsula_peds.csv'

print('Reading edges...')
edges = gpd.read_file(edges_path)
print('edges columns:', edges.columns.tolist())
if 'osmid' in edges.columns:
    print('osmid dtype:', edges['osmid'].dtype)
    print('osmid sample values:', edges['osmid'].dropna().unique()[:10])
    print('osmid nulls:', int(edges['osmid'].isna().sum()))
else:
    print('osmid column not present')

print('\nReading strava csv...')
strava = pd.read_csv(strava_path, low_memory=False)
print('strava columns:', strava.columns.tolist())
if 'osm_reference_id' in strava.columns:
    print('osm_reference_id dtype:', strava['osm_reference_id'].dtype)
    print('osm_reference_id sample values:', pd.Series(strava['osm_reference_id'].dropna().unique())[:10].tolist())
    print('osm_reference_id nulls:', int(strava['osm_reference_id'].isna().sum()))
    # Try conversion to int64 safely
    conv = pd.to_numeric(strava['osm_reference_id'], errors='coerce')
    print('convertible count (non-null after coercion):', int(conv.notna().sum()))
    print('sample converted (first 10 unique):', pd.Series(conv.dropna().astype('int64').unique())[:10].tolist())
else:
    print('osm_reference_id not present')

print('\nDone')
