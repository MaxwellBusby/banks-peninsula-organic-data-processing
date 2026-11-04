## ============================================================
## GWNBR full run
## ============================================================

library(sf)
library(dplyr)
library(mgwnbr)
library(MASS)

## ---- 1. Get the source and apply all five patches --------------------------
fn <- getAnywhere("mgwnbr")$objs[[1]]
writeLines(deparse(fn), "mgwnbr_source.txt")
src <- readLines("mgwnbr_source.txt")
src_txt <- paste(src, collapse = "\n")

# Patch 1: redundant j-loop
patched_txt <- gsub("for \\(j in 1:N\\) \\{", "for (j in 1:1) {", src_txt)
stopifnot(!grepl("for \\(j in 1:N\\)", patched_txt))

# Patch 2: guard NaN significance-test comparisons
patched_txt <- gsub(
  "probt\\[i, j\\] < ",
  "!is.na(probt[i, j]) && probt[i, j] < ",
  patched_txt
)
patched_txt <- gsub(
  "aprobtstat\\[i\\] < ",
  "!is.na(aprobtstat[i]) && aprobtstat[i] < ",
  patched_txt
)

patched_txt <- gsub(
  "mf <- eval(mf)",
  "mf <- eval(mf, parent.frame())",
  patched_txt,
  fixed = TRUE
)
stopifnot(grepl("eval(mf, parent.frame())", patched_txt, fixed = TRUE))

# Progress print
patched_txt <- sub(
  "cv <- function(H, y, x, fi) {\n        nvar <- ncol(x)\n        for (i in 1:N) {",
  "cv <- function(H, y, x, fi) {\n        nvar <- ncol(x)\n        for (i in 1:N) {\n            if (i %% 250 == 0) cat('cv sweep: i =', i, 'of', N, format(Sys.time()), '\\n')",
  patched_txt, fixed = TRUE
)

## Keep X[, -1] as a matrix when there is only one predictor
n_before <- lengths(regmatches(patched_txt, gregexpr("X[, -1]", patched_txt, fixed = TRUE)))
patched_txt <- gsub("X[, -1]", "X[, -1, drop = FALSE]", patched_txt, fixed = TRUE)
if (n_before != 3) {
  warning("Expected 3 occurrences of X[, -1] in mgwnbr source")
}

mgwnbr_patched3 <- eval(parse(text = patched_txt), envir = asNamespace("mgwnbr"))
environment(mgwnbr_patched3) <- asNamespace("mgwnbr")

stopifnot(!grepl("for \\(j in 1:N\\)", paste(deparse(mgwnbr_patched3), collapse = "\n")))
stopifnot(grepl("!is.na\\(probt", paste(deparse(mgwnbr_patched3), collapse = "\n")))


# Patch 5: safe_solve — catch "computationally singular" errors from solve()
safe_solve <- function(a, b, tol = .Machine$double.eps, ...) {
  b_missing <- missing(b)
  tryCatch({
    if (b_missing) base::solve(a, tol = tol, ...) else base::solve(a, b, tol = tol, ...)
  }, error = function(e) {
    if (grepl("singular", conditionMessage(e), ignore.case = TRUE)) {
      if (b_missing) {
        matrix(0, nrow(a), ncol(a))
      } else if (is.matrix(b)) {
        matrix(0, ncol(a), ncol(b))
      } else {
        rep(0, ncol(a))
      }
    } else {
      stop(e)
    }
  })
}

solve_wrap_env <- new.env(parent = environment(mgwnbr_patched3))
assign("solve", safe_solve, envir = solve_wrap_env)
environment(mgwnbr_patched3) <- solve_wrap_env
message("Patch 5 (safe_solve) applied.")

## ---- 2. Read and prep data --------------------------------------------------
gpkg_path <- "post_collinearity_features_bike.gpkg"

st_layers(gpkg_path)
bike <- st_read(gpkg_path)

y_var <- "count"

x_vars <- c(
  "trail_density_400m",
  "elevation_range",
  "elevation_max"
)

bike_coords <- st_coordinates(st_centroid(st_geometry(bike)))

bike_df <- bike %>%
  st_drop_geometry() %>%
  mutate(
    long = bike_coords[, 1],
    lat  = bike_coords[, 2]
  ) %>%
  dplyr::select(all_of(c(y_var, x_vars, "long", "lat")))

bike_df$row_id <- seq_len(nrow(bike_df))

n_before <- nrow(bike_df)
bike_df <- bike_df %>%
  filter(if_all(all_of(c(y_var, x_vars, "long", "lat")), ~ !is.na(.)))
n_after <- nrow(bike_df)
if (n_after < n_before) {
  message(sprintf("Dropbike %d rows with missing values (%d -> %d).",
                  n_before - n_after, n_before, n_after))
}

bike_df <- as.data.frame(bike_df)
gwnbr_formula <- as.formula(
  paste(y_var, "~", paste(x_vars, collapse = " + "))
)

## ---- Output naming --------------
run_id  <- format(Sys.time(), "%Y%m%d_%H%M%S")
out_dir <- file.path("./Outputs/GWNBR", run_id)
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
message("Output folder for this run: ", out_dir)

## ---- 3. Global glm.nb() ----------------------------------------
message("Fitting global glm.nb() sanity check...")
global_nb <- MASS::glm.nb(gwnbr_formula, data = bike_df)
message("Global glm.nb() AIC: ", AIC(global_nb))
print(summary(global_nb))

## ---- 4. Full model fit -------------
message("Starting full GWNBR fit (CV bandwidth search) at ", format(Sys.time()))
t0 <- Sys.time()

gwnbr_mod <- suppressWarnings(
  mgwnbr_patched3(
    data           = bike_df,
    formula        = gwnbr_formula,
    long           = "long",
    lat            = "lat",
    band_method    = "adaptive_bsq",
    band_criterion = "cv",
    distribution   = "negbin",
    globalmin      = FALSE,
    multiscale     = FALSE,
    distancekm     = FALSE,
    id             = "row_id",
    tol            = 1e-6
  )
)

t1 <- Sys.time()
message("Finished full GWNBR fit at ", format(t1), "  elapsed: ", format(t1 - t0))
message("Selected bandwidth: ", gwnbr_mod$general_bandwidth)
message("AICc: ", gwnbr_mod$measures["AICc"])

## ---- 5. Save  -----------------------------------------------------
model_rds_path <- file.path(out_dir, "gwnbr_full_result.rds")
saveRDS(gwnbr_mod, model_rds_path)
message("Saved model object to ", model_rds_path)

## ---- 6. Rejoin local estimates onto the spatial layer and save a gpkg -------
local_estimates <- as.data.frame(gwnbr_mod$mgwr_param_estimates)
names(local_estimates) <- paste0("b_", names(local_estimates))
local_se <- as.data.frame(gwnbr_mod$mgwr_se)
names(local_se) <- paste0("se_", names(local_se))

results_df <- bind_cols(
  row_id = bike_df$row_id,
  fitted = gwnbr_mod$fitted_values,
  local_estimates,
  local_se
)

bike_results <- bike %>%
  mutate(row_id = seq_len(nrow(bike))) %>%
  inner_join(results_df, by = "row_id")

gpkg_out_path <- file.path(out_dir, "bike_gwnbr_results_3.gpkg")
st_write(
  bike_results,
  gpkg_out_path,
  layer = "bike_gwnbr",
  delete_layer = TRUE
)
message("Saved spatial results to ", gpkg_out_path)
message("ALL DONE at ", format(Sys.time()))

