## ============================================================
## Forward tep-wise Feature Selection for GWNBR — checkpointed version
## ============================================================

library(sf)
library(dplyr)
library(mgwnbr)
library(foreach)
library(doParallel)

## ============================================================
## PART 1: Fix mgwnbr function to allow for single variable runs
## ============================================================
build_patched_mgwnbr <- function() {
  fn <- getAnywhere("mgwnbr")$objs[[1]]
  src_txt <- paste(deparse(fn), collapse = "\n")
  
  patched_txt <- gsub("for \\(j in 1:N\\) \\{", "for (j in 1:1) {", src_txt)
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
  
  ## Keep X[, -1] as a matrix when there is only one predictor
  n_before <- lengths(regmatches(patched_txt, gregexpr("X[, -1]", patched_txt, fixed = TRUE)))
  patched_txt <- gsub("X[, -1]", "X[, -1, drop = FALSE]", patched_txt, fixed = TRUE)
  if (n_before != 3) {
    warning("Expected 3 occurrences of X[, -1] in mgwnbr source, found ", n_before,
            " - the installed mgwnbr version may differ from the one this patch was written for.")
  }
  
  mgwnbr_patched <- eval(parse(text = patched_txt), envir = asNamespace("mgwnbr"))
  environment(mgwnbr_patched) <- asNamespace("mgwnbr")
  
  safe_solve <- function(a, b, tol = .Machine$double.eps, ...) {
    b_missing <- missing(b)
    tryCatch({
      if (b_missing) base::solve(a, tol = tol, ...) else base::solve(a, b, tol = tol, ...)
    }, error = function(e) {
      if (grepl("singular", conditionMessage(e), ignore.case = TRUE)) {
        if (b_missing) matrix(0, nrow(a), ncol(a))
        else if (is.matrix(b)) matrix(0, ncol(a), ncol(b))
        else rep(0, ncol(a))
      } else {
        stop(e)
      }
    })
  }
  
  solve_wrap_env <- new.env(parent = environment(mgwnbr_patched))
  assign("solve", safe_solve, envir = solve_wrap_env)
  environment(mgwnbr_patched) <- solve_wrap_env
  
  
  mgwnbr_patched
}


## ============================================================
## PART 2: Checkpoint file naming
## ============================================================
make_checkpoint_path <- function(checkpoint_dir, selected_vars, candidate_var) {
  sig <- if (length(selected_vars) == 0) "BASE" else paste(sort(selected_vars), collapse = "+")
  sig <- gsub("[^A-Za-z0-9+]", "_", sig)
  
  # Guard against overly long filenames (many selected vars) — fall back to
  # a short checksum of the signature if it gets unwieldy, while still
  # keeping filenames deterministic and collision-resistant enough for
  # this use case (a handful of candidate combinations, not millions).
  if (nchar(sig) > 120) {
    sig <- sprintf("sig%08x", sum(utf8ToInt(sig)) %% .Machine$integer.max)
  }
  
  cand_clean <- gsub("[^A-Za-z0-9]", "_", candidate_var)
  file.path(checkpoint_dir, paste0(sig, "__", cand_clean, ".rds"))
}

## ============================================================
## PART 3: Fit one candidate, with checkpoint read/write
## ============================================================
fit_one_candidate <- function(candidate_var, selected_vars, y_var, data,
                              long_col, lat_col, id_col,
                              band_method, bandwidth_mode, fixed_h,
                              band_criterion, tol, checkpoint_dir) {
  
  ckpt_path <- make_checkpoint_path(checkpoint_dir, selected_vars, candidate_var)
  
  # If this exact (selected_vars, candidate) combination has already been
  # computed — in this run, a previous run, or on another node sharing the
  # same checkpoint directory — reuse it instead of recomputing.
  if (file.exists(ckpt_path)) {
    cached <- tryCatch(readRDS(ckpt_path), error = function(e) NULL)
    if (!is.null(cached)) {
      cached$from_cache <- TRUE
      return(cached)
    }
    # Corrupt/unreadable checkpoint — fall through and recompute.
  }
  
  mgwnbr_fn <- build_patched_mgwnbr()
  
  this_vars <- c(selected_vars, candidate_var)
  this_formula <- as.formula(paste(y_var, "~", paste(this_vars, collapse = " + ")))
  
  result <- tryCatch({
    args <- list(
      data = data, formula = this_formula, long = long_col, lat = lat_col,
      band_method = band_method, band_criterion = band_criterion,
      distribution = "negbin", globalmin = FALSE, multiscale = FALSE,
      distancekm = FALSE, id = id_col, tol = tol
    )
    if (identical(bandwidth_mode, "fixed")) args$h <- fixed_h
    m <- suppressWarnings(do.call(mgwnbr_fn, args))
    
    fv <- m$fitted_values$yhat
    n_nonfinite <- sum(!is.finite(fv))
    coef_mat <- as.matrix(m$mgwr_param_estimates[, -1])
    zero_frac <- mean(rowSums(coef_mat == 0) == ncol(coef_mat))
    
    list(
      status = if (n_nonfinite == 0) "ok" else "unstable_nonfinite_yhat",
      formula = paste(deparse(this_formula), collapse = " "),
      bandwidth = m$general_bandwidth,
      AIC = unname(m$measures["AIC"]),
      AICc = unname(m$measures["AICc"]),
      pseudo_R2 = unname(m$measures["adjpctdev"]),
      n_nonfinite = n_nonfinite,
      zero_frac = zero_frac,
      error_msg = NA_character_
    )
  }, error = function(e) {
    list(
      status = "error",
      formula = paste(deparse(this_formula), collapse = " "),
      bandwidth = NA_real_, AIC = NA_real_, AICc = NA_real_, pseudo_R2 = NA_real_,
      n_nonfinite = NA_integer_, zero_frac = NA_real_,
      error_msg = conditionMessage(e)
    )
  })
  
  result$candidate_var <- candidate_var
  result$from_cache <- FALSE
  
  # Save to disk IMMEDIATELY, inside the worker, before returning — this is
  # what survives a sibling worker crashing later in the same foreach batch.
  saveRDS(result, ckpt_path)
  
  result
}

## ============================================================
## PART 4: Main forward selection driver
## ============================================================
gwr_forward_selection <- function(
    data,
    y_var,
    candidate_vars,
    long_col = "long",
    lat_col  = "lat",
    id_col   = "row_id",
    band_method    = "adaptive_bsq",
    band_criterion = "cv",
    bandwidth_mode = c("fixed", "cv"),
    fixed_h        = NULL,
    tol            = 1e-6,
    n_cores        = max(1, parallel::detectCores() - 1),
    log_path       = "forward_selection_log.csv",
    checkpoint_dir = "forward_selection_checkpoints",
    resume_log_path = NULL,
    stop_on_no_improvement = FALSE,
    delta_aic_threshold   = 2,
    max_iterations = length(candidate_vars)
) {
  bandwidth_mode <- match.arg(bandwidth_mode)
  if (bandwidth_mode == "fixed" && is.null(fixed_h)) {
    stop("fixed_h must be supplied when bandwidth_mode = 'fixed'.")
  }
  if (bandwidth_mode == "cv") {
    message("WARNING: bandwidth_mode = 'cv' means a full CV bandwidth search ",
            "per candidate, per iteration. Expect this to be extremely slow ",
            "at full N - consider 'fixed' unless you have a lot of time.")
  }
  
  dir.create(checkpoint_dir, recursive = TRUE, showWarnings = FALSE)
  
  log_cols <- c("Iteration", "Tested_Variable", "Current_Model_Formula",
                "Bandwidth", "AIC", "AICc", "R2", "Status", "Error_Msg",
                "Status_Selected")
  
  selected_vars   <- character(0)
  aic_history    <- numeric(0)
  iteration_start <- 1
  
  if (!is.null(resume_log_path)) {
    stopifnot(file.exists(resume_log_path))
    prior_log <- read.csv(resume_log_path, stringsAsFactors = FALSE)
    
    is_selected <- trimws(toupper(prior_log$Status_Selected)) == "SELECTED"
    selected_rows <- prior_log[is_selected, ]
    selected_rows <- selected_rows[order(selected_rows$Iteration), ]
    
    selected_vars   <- selected_rows$Tested_Variable
    aic_history    <- selected_rows$AIC
    iteration_start <- max(prior_log$Iteration, na.rm = TRUE) + 1
    
    message("Resuming from ", resume_log_path)
    message("Already selected (", length(selected_vars), "): ",
            paste(selected_vars, collapse = ", "))
    message("Resuming at iteration ", iteration_start)
    
    log_path <- resume_log_path
  } else {
    write.csv(data.frame(matrix(ncol = length(log_cols), nrow = 0,
                                dimnames = list(NULL, log_cols))),
              log_path, row.names = FALSE)
  }
  
  remaining_vars <- setdiff(candidate_vars, selected_vars)
  prev_best_aic <- if (length(aic_history) > 0) tail(aic_history, 1) else Inf
  
  make_cluster <- function() {
    cl <- parallel::makeCluster(n_cores)
    doParallel::registerDoParallel(cl)
    cl
  }
  cl <- make_cluster()
  on.exit(try(parallel::stopCluster(cl), silent = TRUE), add = TRUE)
  
  iteration <- iteration_start - 1
  repeat {
    iteration <- iteration + 1
    
    if (length(remaining_vars) == 0) {
      message("Stopping: all candidate variables have been used or exhausted.")
      break
    }
    if (iteration > max_iterations) {
      message("Stopping: max_iterations reached.")
      break
    }
    
    message(sprintf("=== Iteration %d: testing %d candidate(s): %s ===",
                    iteration, length(remaining_vars),
                    paste(remaining_vars, collapse = ", ")))
    
    ## ---- Run this iteration's candidates in parallel, but tolerate a
    ## dead worker: if the whole foreach call errors out (e.g. the
    ## "unserialize" socket-death crash), don't lose everything — fall
    ## through and recover whatever checkpoint files DID get written by
    ## workers that finished before the crash.
    foreach_error <- NULL
    iter_results <- tryCatch({
      foreach(
        v = remaining_vars,
        .packages = c("mgwnbr"),
        .export   = c("build_patched_mgwnbr", "fit_one_candidate", "make_checkpoint_path")
      ) %dopar% {
        fit_one_candidate(
          candidate_var = v, selected_vars = selected_vars, y_var = y_var,
          data = data, long_col = long_col, lat_col = lat_col, id_col = id_col,
          band_method = band_method, bandwidth_mode = bandwidth_mode,
          fixed_h = fixed_h, band_criterion = band_criterion, tol = tol,
          checkpoint_dir = checkpoint_dir
        )
      }
    }, error = function(e) {
      foreach_error <<- conditionMessage(e)
      NULL
    })
    
    if (!is.null(foreach_error)) {
      message("Parallel batch errored (likely a dead worker): ", foreach_error)
      message("Recovering from checkpoint files on disk instead of losing this iteration...")
      # The cluster may be in a bad state after a worker died — rebuild it
      # before continuing to the next iteration.
      try(parallel::stopCluster(cl), silent = TRUE)
      cl <- make_cluster()
    }
    
    ## ---- Recover results for THIS iteration's candidates by reading
    ## checkpoint files directly, regardless of whether foreach itself
    ## succeeded — this is the single source of truth, not iter_results.
    iter_list <- lapply(remaining_vars, function(v) {
      ckpt_path <- make_checkpoint_path(checkpoint_dir, selected_vars, v)
      if (file.exists(ckpt_path)) {
        r <- tryCatch(readRDS(ckpt_path), error = function(e) NULL)
        if (!is.null(r)) return(r)
      }
      # No checkpoint exists — this candidate's worker never finished
      # (crashed before it could save). Log it as a distinct failure type
      # so it's clearly not the same as a genuine model instability.
      list(
        candidate_var = v, status = "error",
        formula = paste(deparse(as.formula(paste(y_var, "~", paste(c(selected_vars, v), collapse = " + ")))), collapse = " "),
        bandwidth = NA_real_, AIC = NA_real_, AICc = NA_real_, pseudo_R2 = NA_real_,
        n_nonfinite = NA_integer_, zero_frac = NA_real_,
        error_msg = "no checkpoint produced - worker likely crashed before finishing"
      )
    })
    
    iter_df <- do.call(rbind, lapply(iter_list, function(r) {
      data.frame(
        Iteration = iteration, Tested_Variable = r$candidate_var,
        Current_Model_Formula = r$formula, Bandwidth = r$bandwidth,
        AIC = r$AIC, AICc = r$AICc, R2 = r$pseudo_R2,
        Status = r$status, Error_Msg = r$error_msg,
        stringsAsFactors = FALSE
      )
    }))
    
    valid <- iter_df[iter_df$Status == "ok" & is.finite(iter_df$AIC), ]
    
    if (nrow(valid) == 0) {
      message("Stopping: every remaining candidate failed or was unstable ",
              "this iteration — no more features can be added.")
      iter_df$Status_Selected <- "NOT_SELECTED_ALL_FAILED"
      write.table(iter_df[, log_cols], log_path, sep = ",", append = TRUE,
                  row.names = FALSE, col.names = FALSE)
      break
    }
    
    best_row  <- valid[which.min(valid$AIC), ]
    best_var  <- best_row$Tested_Variable
    best_aic <- best_row$AIC
    
    iter_df$Status_Selected <- ifelse(iter_df$Tested_Variable == best_var,
                                      "SELECTED", "not_selected")
    
    write.table(iter_df[, log_cols], log_path, sep = ",", append = TRUE,
                row.names = FALSE, col.names = FALSE)
    
    n_from_cache <- sum(vapply(iter_list, function(r) isTRUE(r$from_cache), logical(1)))
    message(sprintf("  Best this iteration: %s (AIC = %.2f) [%d/%d candidates reused from cache]",
                    best_var, best_aic, n_from_cache, length(remaining_vars)))
    
    if (stop_on_no_improvement) {
      improvement <- prev_best_aic - best_aic
      if (is.finite(prev_best_aic) && improvement < delta_aic_threshold) {
        message(sprintf(
          "Stopping: AIC improvement (%.3f) below threshold (%.3f).",
          improvement, delta_aic_threshold
        ))
        break
      }
    }
    
    selected_vars  <- c(selected_vars, best_var)
    remaining_vars <- setdiff(remaining_vars, best_var)
    aic_history   <- c(aic_history, best_aic)
    prev_best_aic <- best_aic
  }
  
  if (length(selected_vars) == 0) {
    warning("No variables were successfully selected.")
    return(list(selected_vars = character(0), final_formula = NULL,
                aic_history = aic_history, log_path = log_path,
                checkpoint_dir = checkpoint_dir))
  }
  
  final_formula <- as.formula(paste(y_var, "~", paste(selected_vars, collapse = " + ")))
  
  message("\n=== Forward selection complete ===")
  message("Selected variables: ", paste(selected_vars, collapse = ", "))
  message("Final formula: ", paste(deparse(final_formula), collapse = " "))
  message("AIC progression: ", paste(round(aic_history, 2), collapse = " -> "))
  
  list(
    selected_vars = selected_vars,
    final_formula = final_formula,
    aic_history  = aic_history,
    log_path      = log_path,
    checkpoint_dir = checkpoint_dir
  )
}

## ============================================================
## PART 5: Input
## ============================================================

gpkg_path <- "post_collinearity_features_ped.gpkg"

st_layers(gpkg_path)
df <- st_read(gpkg_path, layer = "post_collinearity_features_ped")
df_coords <- st_coordinates(st_centroid(st_geometry(df)))

candidate_vars <- c("elevation_range", "elevation_max", "gradient",
                    "Rural_settlement_distances", "Major_urban_distances",
                    "trail_density_400m", "degree_centrality", "toilets_distance",
                    "closeness_centrality", "eateries_distance", "parking_distance", "distance_to_road")

feature_df <- df %>%
  st_drop_geometry() %>%
  mutate(
    long = df_coords[, 1],
    lat  = df_coords[, 2]
  ) %>%
  dplyr::select(all_of(c("count", candidate_vars, "long", "lat")))
feature_df$row_id <- seq_len(nrow(feature_df))
feature_df <- as.data.frame(feature_df)
# )

SAMPLE_SIZE <- NULL   # <- set to NULL to run on the full dataset
if (!is.null(SAMPLE_SIZE) && SAMPLE_SIZE < nrow(feature_df)) {
  set.seed(1)
  feature_df <- feature_df[sample(nrow(feature_df), SAMPLE_SIZE), ]
  feature_df <- as.data.frame(feature_df)
  message("SAMPLE_SIZE set: running on ", nrow(feature_df), " of the full dataset.")
}
  result <- gwr_forward_selection(
    data = feature_df, y_var = "count", candidate_vars = candidate_vars,
    long_col = "long", lat_col = "lat", id_col = "row_id",
    bandwidth_mode = "cv", fixed_h = 150, tol = 1e-6,
    n_cores = 13,
    log_path = "./Outputs/GWNBR/fvs_ped_1.csv",
    #resume_log_path = "./Outputs/GWNBR/fvs_ped_1.csv",
    checkpoint_dir = "./Outputs/GWNBR/fvs_ped_1_checkpoints"
  )