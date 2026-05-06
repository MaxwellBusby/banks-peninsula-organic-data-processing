# coding: utf-8
import geopandas as gpd
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import csv
import cartopy.crs as ccrs
from shapely.geometry import Point
from shapely.geometry import LineString
import shapely.wkt
import movingpandas as mpd
from shapely.geometry import Point
from datetime import timedelta
import trackintel as ti
import glob
file_path = '../Data/GPS_SPlit/gps_chunk_*.gpkg '
gpkg_files = glob.glob(file_path)
file_path = '../Data/GPS_SPlit/gps_chunk_*.gpkg '
gpkg_files = glob.glob(file_path)
gdf_list = [gpd.read_file(filename) for filename in gpkg_files]
file_path = '../Data/GPS_SPlit/gps_chunk_*.gpkg '
gpkg_files = glob.glob(file_path)
gdf_list = [gpd.read_file(filename) for filename in gpkg_files]
gps_points= pd.concat(gdf_list, ignore_index=True)
gps_sample_IDs = gps_points_chunk_1["Hashed Device ID"].sample(n=50, random_state=1)
gps_samples_gdf = gps_points_chunk_1[gps_points_chunk_1["Hashed Device ID"].isin(gps_sample_IDs)]
gps_points= pd.concat(gdf_list, ignore_index=True)
file_path = '../Data/GPS_SPlit/gps_chunk_*.gpkg '
gpkg_files = glob.glob(file_path)
gdf_list = [gpd.read_file(filename) for filename in gpkg_files]
gps_points= pd.concat(gdf_list, ignore_index=True)
file_path = '../Data/GPS_SPlit/*.gpkg '
gpkg_files = glob.glob(file_path)
gdf_list = [gpd.read_file(filename) for filename in gpkg_files]
gps_points= pd.concat(gdf_list, ignore_index=True)
gpkg_files
file_path = '../Data/GPS_SPlit/*.gpkg'
gpkg_files = glob.glob(file_path)
gdf_list = [gpd.read_file(filename) for filename in gpkg_files]
gps_points= pd.concat(gdf_list, ignore_index=True)
gps_points.to_file("../Data/GPS_All.gpkg", driver="GPKG")
gps_transport_mode = gpd.read_file("../Data/GPS_Sample_Triplegs_Mode.gpkg")
gps_transport_mode = gpd.read_file("/Data/GPS_Sample_Triplegs_Mode.gpkg")
gps_transport_mode = gpd.read_file("Data/GPS_Sample_Triplegs_Mode.gpkg")
trips_for_matching = gps_transport_mode[gps_transport_mode["mode"]=="slow_mobility"].to_crs(epsg=2193)
trips_for_matching.drop(columns=["user_id","started_at","finished_at","mode"], inplace=True)
trips_for_matching.to_file("/Data/Trips_for_Matching.gpkg", driver="GPKG")
trips_for_matching.to_file("Data/Trips_for_Matching.gpkg", driver="GPKG")
trips_for_matching["geometry"] = trips_for_matching.geometry.to_wkt()
trips_for_matching.to_csv(
    # r"\\wsl$\Ubuntu\home\maxwell\fmm\build\python\Data\trips_for_matching.csv",
    "../Data/trips_for_matching.csv",
    sep=";",
    index=False,
    quoting=csv.QUOTE_NONE,
    escapechar="\\",
    lineterminator="\n"
)
trips_for_matching.to_csv(
    # r"\\wsl$\Ubuntu\home\maxwell\fmm\build\python\Data\trips_for_matching.csv",
    "Data/trips_for_matching.csv",
    sep=";",
    index=False,
    quoting=csv.QUOTE_NONE,
    escapechar="\\",
    lineterminator="\n"
)
gps_transport_mode = gpd.read_file("Data/GPS_Sample_Triplegs_Mode.gpkg")
