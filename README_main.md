# AutoML Pipeline – `main.py`

This README explains how to use the `main.py` script in this project. It’s an AutoML-style pipeline that can handle **supervised** (classification/regression) and **unsupervised** (clustering) tasks on a CSV dataset.fileciteturn0file0

---

## 1. What the script does

`main.py` will:

- Load a **CSV dataset** from a path you provide at runtime.
- Optionally use a **target column** (for supervised learning).
- Automatically decide if the task is:
  - **Classification** – if the target has ≤ 10 unique values.
  - **Regression** – if the target has > 10 unique values.
  - **Unsupervised / Clustering** – if no target is provided.
- Perform preprocessing:
  - Label‑encode categorical columns.
  - Median imputation for missing values.
  - Outlier removal using IQR.
  - Robust scaling of features.
  - Automatic feature selection with a Random Forest model (for supervised tasks).
  - SMOTE oversampling for **imbalanced classification**.
- Run AutoML‑style model selection:
  - **Supervised models**:
    - Random Forest (Classifier/Regressor)
    - XGBoost (Classifier/Regressor)
    - LightGBM (Classifier/Regressor)
    - CatBoost (Classifier/Regressor)
    - Deep Neural Network (TensorFlow/Keras)
  - **Unsupervised models**:
    - KMeans
    - DBSCAN
    - Gaussian Mixture Model (GMM)
    - Agglomerative Clustering
- Tune hyperparameters with **Optuna**:
  - 20 trials per tree/boosting model.
  - 10 trials for the DNN.
- Save outputs:
  - Best model + scaler in the `models/` folder.
  - Predictions (or cluster labels) in the `reports/` folder.fileciteturn0file0

---

## 2. Requirements

### 2.1. Python version

- Python **3.8+** is recommended.

### 2.2. Python libraries

Install dependencies (adjust if you already have some of these):

```bash
pip install   pandas numpy scikit-learn imbalanced-learn optuna   xgboost lightgbm catboost tensorflow joblib
```

> Note:  
> - `imbalanced-learn` provides **SMOTE**.  
> - `tensorflow` includes Keras (used for the DNN).  
> - GPU-accelerated versions of TensorFlow / XGBoost / LightGBM are optional but can speed things up.

---

## 3. Project structure

When you run `main.py`, it will ensure these folders exist:

```text
your_project/
├─ main.py
├─ models/          # Saved models and scalers
└─ reports/         # CSV reports with predictions / cluster labels
```

You provide the **dataset path** at runtime; it can live anywhere accessible from where you run the script.

---

## 4. Running the script

From the terminal, in the folder containing `main.py`:

```bash
python main.py
```

You’ll see two interactive prompts:

1. **Dataset path**

   ```text
   Enter dataset file path:
   ```

   - Enter the full or relative path to a **CSV file**, e.g.:

     ```text
     data/my_dataset.csv
     ```

2. **Target column**

   ```text
   Enter target column name (leave blank if unsupervised):
   ```

   - **Supervised mode**:
     - Type the exact name of the target column in your CSV (e.g. `label` or `price`).
   - **Unsupervised mode**:
     - Just press **Enter** with no text.

The script will print:

- Whether it is running in **Supervised** or **Unsupervised** mode.
- If supervised, whether it’s treating the problem as **classification** or **regression**.
- Status messages such as:
  - “Dataset Loaded Successfully”
  - “Preprocessing Complete”
  - Model evaluation & tuning logs.

---

## 5. Supervised mode (classification/regression)

If you provided a target column that exists in the dataset:

1. The script will:
   - Separate features `X` and target `y`.
   - Detect task type:
     - **Classification** if `y.nunique() <= 10`.
     - **Regression** otherwise.
   - Scale features and apply feature selection with a Random Forest.
   - Apply **SMOTE** if task is classification.

2. It then runs Optuna tuning for these candidates:

   - RandomForest
   - XGBoost
   - LightGBM
   - CatBoost
   - DNN (Keras)

   Using:
   - **Accuracy** for classification.
   - **Negative mean squared error** for regression.fileciteturn0file0

3. The best model is retrained on the full processed dataset:

   - **DNN**:
     - Trained for 30 epochs.
     - Saved to: `models/final_model_dnn.keras`
   - **Tree/boosting models**:
     - Retrained with best hyperparameters.
     - Saved to: `models/final_model_<ModelName>.pkl`

4. Predictions are saved to:

```text
reports/final_supervised_predictions.csv
```

with columns:

- `y_true` – the true target values.
- `y_pred` – model predictions.

The fitted **RobustScaler** used for features is saved as:

```text
models/scaler.pkl
```

---

## 6. Unsupervised mode (clustering)

If you leave the target name blank (or the target isn’t found in the columns):

1. The script runs in **unsupervised** mode.
2. It preprocesses the data similarly:
   - Label encoding, imputation, outlier removal, robust scaling (no feature selection or SMOTE since there is no target).fileciteturn0file0
3. It evaluates several clustering algorithms:
   - KMeans
   - DBSCAN
   - GaussianMixture (GMM)
   - AgglomerativeClustering
4. For each, it computes the **silhouette score** and selects the best algorithm.
5. Cluster labels from the best algorithm are saved to:

```text
reports/final_unsupervised_predictions.csv
```

with column:

- `Cluster_Label`

---

## 7. Notes & tips

- **Target detection logic**  
  Classification is assumed when the target has 10 or fewer unique values. If your regression target is discrete but many-valued, this is usually fine. If you have a small-numbered regression target, you may want to manually adapt the logic in the script.

- **Missing values**  
  All features are imputed with the **median**. This is simple and robust but may not be ideal for every dataset.

- **Outlier removal**  
  Uses the IQR (interquartile range) rule across all numeric columns. Aggressive outlier removal can sometimes drop too many rows, so review if needed.

- **SMOTE**  
  Only applied for **classification**. If your dataset is huge, SMOTE may increase runtime.

- **Runtime**  
  Multiple models × multiple Optuna trials can be computationally heavy. Reduce:
  - Number of trials per model.
  - Number of models in the `models` list.
  - DNN epochs.

---

## 8. Extending / customizing

You can modify `main.py` to:

- Add/remove models from the `models` list in supervised mode.
- Adjust Optuna trial counts or parameter ranges.
- Change the criteria for **classification vs regression** detection.
- Extend preprocessing (e.g. custom encoders, scaling, or feature engineering).
- Change clustering algorithms or number of clusters (currently 3 for KMeans/GMM/Agglomerative).

---

## 9. Outputs summary

After running, check:

- `models/`
  - `scaler.pkl` – RobustScaler for feature scaling.
  - `final_model_dnn.keras` or `final_model_<ModelName>.pkl` – best trained model.

- `reports/`
  - `final_supervised_predictions.csv` – for supervised tasks.
  - `final_unsupervised_predictions.csv` – for unsupervised tasks.

These can be used for evaluation, analysis, or deployment as needed.

---

Happy experimenting with your AutoML pipeline! 🚀
