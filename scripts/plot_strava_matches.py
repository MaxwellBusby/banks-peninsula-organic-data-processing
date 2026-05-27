import geopandas as gpd
import matplotlib.pyplot as plt
import os


def main():
    strava_fp = r'Data/Strava/strava_bikeped_osm.gpkg'
    matched_fp = r'Data/Final_Matched.gpkg'

    print('Reading Strava geometries from', strava_fp)
    s = gpd.read_file(strava_fp)
    print('Strava rows:', len(s))

    print('Reading matched geometries from', matched_fp)
    try:
        m = gpd.read_file(matched_fp)
        print('Matched rows:', len(m))
    except Exception as e:
        print('Failed reading matched file:', e)
        m = gpd.GeoDataFrame(columns=['geometry'], geometry='geometry', crs=s.crs)

    if s.crs != m.crs:
        try:
            m = m.to_crs(s.crs)
            print('Reprojected matched geometries to', s.crs)
        except Exception:
            print('Could not reproject matched geometries; proceeding with spatial join may fail')

    # Spatial join to detect which Strava geometries intersect matched geometries
    print('Performing spatial join to detect matches...')
    try:
        joined = gpd.sjoin(s, m[['geometry']], how='left', predicate='intersects')
        s['matched'] = ~joined['index_right'].isna()
    except Exception as e:
        print('Spatial join failed:', e)
        # fallback: mark all False
        s['matched'] = False

    outdir = os.path.join('Plots')
    os.makedirs(outdir, exist_ok=True)
    outfp = os.path.join(outdir, 'strava_matches.png')

    fig, ax = plt.subplots(1, 1, figsize=(10, 8))
    s.loc[~s['matched']].plot(ax=ax, color='red', linewidth=1, label='unmatched')
    s.loc[s['matched']].plot(ax=ax, color='green', linewidth=1, label='matched')
    if len(m):
        m.plot(ax=ax, facecolor='none', edgecolor='blue', alpha=0.5, label='matched edges')

    ax.set_title('Strava geometries: matched (green) vs unmatched (red)')
    ax.legend()
    plt.tight_layout()
    plt.savefig(outfp, dpi=150)
    print('Saved plot to', outfp)


if __name__ == '__main__':
    main()
