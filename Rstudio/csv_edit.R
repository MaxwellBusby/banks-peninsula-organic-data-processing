## ============================================================
## Safe log-editing helpers — inspect and modify forward-selection log
## CSVs entirely within R, avoiding the hidden-corruption issues that
## come from editing the raw CSV text or opening it in Excel.
## ============================================================

## ---- 1. INSPECT: reveal hidden whitespace / type issues in a column -------
inspect_log <- function(log_path) {
  log <- read.csv(log_path, stringsAsFactors = FALSE, colClasses = "character")
  
  cat("=== Column types as read ===\n")
  print(sapply(log, class))
  
  cat("\n=== Status_Selected: raw vs trimmed comparison ===\n")
  raw <- log$Status_Selected
  trimmed <- trimws(raw)
  mismatch <- raw != trimmed
  if (any(mismatch, na.rm = TRUE)) {
    cat("Found", sum(mismatch, na.rm = TRUE), "rows with leading/trailing whitespace:\n")
    print(data.frame(Row = which(mismatch), Raw = sprintf("[%s]", raw[mismatch])))
  } else {
    cat("No whitespace issues found in Status_Selected.\n")
  }
  
  cat("\n=== Distinct values in Status_Selected (exact, incl. whitespace) ===\n")
  print(table(sprintf("[%s]", raw), useNA = "always"))
  
  cat("\n=== Row field-count check (catches shifted/misaligned rows) ===\n")
  raw_lines <- readLines(log_path)
  field_counts <- lengths(strsplit(raw_lines, ","))
  header_n <- field_counts[1]
  bad_rows <- which(field_counts != header_n)
  if (length(bad_rows) > 0) {
    cat("WARNING: rows with a different field count than the header:\n")
    print(bad_rows)
  } else {
    cat("All rows have the expected field count (", header_n, ").\n")
  }
  
  invisible(log)
}

## ---- 2. LOAD: always read with an explicit, consistent spec --------------
load_log <- function(log_path) {
  log <- read.csv(log_path, stringsAsFactors = FALSE)
  # Normalize the columns most prone to hidden whitespace/case issues —
  # safe to do on every load, never changes genuinely correct values.
  log$Status_Selected <- trimws(log$Status_Selected)
  log$Status          <- trimws(log$Status)
  log
}

## ---- 3. SAVE: always write with an explicit, consistent spec -------------
save_log <- function(log, log_path) {
  write.csv(log, log_path, row.names = FALSE, na = "NA")
  message("Saved: ", log_path)
}

## ---- 4. EDIT HELPERS: common operations, done as vectorized R edits ------

# Keep only iterations up to (and including) max_iter
drop_iterations_after <- function(log, max_iter) {
  log[log$Iteration <= max_iter, ]
}

# Drop specific variables' rows entirely, from every iteration
drop_variables <- function(log, vars_to_drop) {
  log[!(log$Tested_Variable %in% vars_to_drop), ]
}

# Ensure exactly one SELECTED row per iteration. For any iteration missing
# one (e.g. because its selected row was just dropped), assign it to the
# row with the lowest AICc among what's left in that iteration (falls back
# to the last remaining row if all AICc values are NA/Inf).
## Reassign missing SELECTED rows (unchanged)
reassign_missing_selections <- function(log) {
  for (it in unique(log$Iteration)) {
    idx <- which(log$Iteration == it)
    has_selected <- any(log$Status_Selected[idx] == "SELECTED")
    if (!has_selected && length(idx) > 0) {
      aiccs <- log$AICc[idx]
      if (any(is.finite(aiccs))) {
        pick <- idx[which.min(aiccs)]
      } else {
        pick <- idx[length(idx)]
      }
      log$Status_Selected[idx] <- "not_selected"
      log$Status_Selected[pick] <- "SELECTED"
      message("Iteration ", it, ": reassigned SELECTED to ", log$Tested_Variable[pick])
    }
  }
  log
}

# Add a new row to the log safely — builds it with the exact column set
# and order the log already has, so it can never introduce the column
# misalignment bug that comes from manually typing/pasting a CSV line.
#
# All fields except Iteration, Tested_Variable are optional and default to
# NA — useful for hand-crafting a synthetic "SELECTED" row when setting up
# a test scenario (e.g. seeding a resume log without having actually run
# the model for that row).
#
# enforce_single_selected: if TRUE (default) and status_selected =
# "SELECTED", any other row already marked SELECTED in the same iteration
# is automatically demoted to "not_selected" first, so you can never end
# up with two SELECTED rows in one iteration by accident.
add_log_row <- function(log,
                        iteration,
                        tested_variable,
                        formula = NA_character_,
                        bandwidth = NA_real_,
                        aic = NA_real_,
                        aicc = NA_real_,
                        r2 = NA_real_,
                        status = "ok",
                        error_msg = NA_character_,
                        status_selected = "not_selected",
                        enforce_single_selected = TRUE) {
  
  expected_cols <- c("Iteration", "Tested_Variable", "Current_Model_Formula",
                     "Bandwidth", "AIC", "AICc", "R2", "Status", "Error_Msg",
                     "Status_Selected")
  missing_cols <- setdiff(expected_cols, names(log))
  if (length(missing_cols) > 0) {
    stop("log is missing expected column(s): ", paste(missing_cols, collapse = ", "))
  }
  
  if (is.na(formula) && length(tested_variable) == 1) {
    # Best-effort default formula: previously selected vars (if any exist
    # in this log) + the new variable. Purely a convenience — override
    # with the `formula` argument if you need the exact real formula.
    prior_selected <- log$Tested_Variable[trimws(log$Status_Selected) == "SELECTED"]
    all_vars <- c(prior_selected, tested_variable)
    formula <- paste("count ~", paste(all_vars, collapse = " + "))
  }
  
  if (identical(status_selected, "SELECTED") && enforce_single_selected) {
    same_iter <- which(log$Iteration == iteration & log$Status_Selected == "SELECTED")
    if (length(same_iter) > 0) {
      message("Demoting ", length(same_iter), " existing SELECTED row(s) in iteration ",
              iteration, " to keep exactly one.")
      log$Status_Selected[same_iter] <- "not_selected"
    }
  }
  
  new_row <- data.frame(
    Iteration = iteration,
    Tested_Variable = tested_variable,
    Current_Model_Formula = formula,
    Bandwidth = bandwidth,
    AIC = aic,
    AICc = aicc,
    R2 = r2,
    Status = status,
    Error_Msg = error_msg,
    Status_Selected = status_selected,
    stringsAsFactors = FALSE
  )
  
  # Force exact same column order as the existing log before binding —
  # this is the step that guarantees no misalignment, regardless of what
  # order the arguments were supplied in above.
  new_row <- new_row[, names(log)]
  
  rbind(log, new_row)
}

