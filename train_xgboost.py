import warnings
warnings.filterwarnings('ignore')

import numpy as  np
import pandas as pd
import os
import json
import gc
from sklearn.preprocessing import LabelEncoder
from sklearn import preprocessing
from sklearn.model_selection import train_test_split
from tqdm import tqdm
from scipy.sparse import hstack, csr_matrix, vstack
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from nltk.corpus import stopwords
import utils
import argparse

import xgboost as xgb

parser = argparse.ArgumentParser()
parser.add_argument('feature', choices=['load', 'new'])
args = parser.parse_args()

# Load config
config = json.load(open("config.json"))
xgb_root = "xgb_root"

nrows = None
sub = pd.read_csv(config["sample_submission"],  nrows=nrows)
len_sub = len(sub)
print("Sample submission len {}".format(len_sub))

if args.feature == "new":
    # Load csv
    print("\n[+] Load csv ")
    train_df = pd.read_csv(config["train_csv"], parse_dates = ["activation_date"], index_col="item_id", nrows=nrows)
    train_df = train_df.replace(np.nan, -1, regex=True)
    test_df = pd.read_csv(config["test_csv"], parse_dates = ["activation_date"], index_col="item_id", nrows=nrows)
    test_df = test_df.replace(np.nan, -1, regex=True)

    user_df = pd.read_csv('./aggregated_features.csv', nrows=nrows)
    # city_region_unique = pd.read_csv('./avito_region_city_features.csv', nrows=nrows)

    # Merge two dataframes
    n_train = len(train_df)
    df = pd.concat([train_df, test_df])
    del train_df, test_df
    gc.collect()
    
    print("before:", df.shape)
    df = pd.merge(left=df, right=user_df, how="left", on=["user_id"])
    print("after :", df.shape)

    # Fillin missing data
    for col in ["description", "title", "param_1", "param_2", "param_3"]:
        df[col] = df[col].fillna(" ")

    df["params"] = df.apply(lambda row: " ".join([
        str(row["param_1"]),
        str(row["param_2"]),
        str(row["param_3"]),
    ]), axis=1)

    # # Fill-in missing value by mean
    # for col in ["price", "image_top_1"]:
    #     m = df[col].mean()
    #     df[col] = df[col].fillna(-1, inplace=True)
    #     df[col] = df[col].replace(-1, m, regex=True)

    # Get log of price
    df["price"] = df["price"].apply(np.log1p)
    df["price"] = df["price"].apply(lambda x: -1 if x == -np.inf else x)

    # df['city'] = df['city'] + '_' + df['region']
    df['no_img'] = pd.isna(df.image).astype(int)
    df['no_dsc'] = pd.isna(df.description).astype(int)
    df['no_p1'] = pd.isna(df.param_1).astype(int)
    df['no_p2'] = pd.isna(df.param_2).astype(int)
    df['no_p3'] = pd.isna(df.param_3).astype(int)
    df['weekday'] = df['activation_date'].dt.weekday
    # df['monthday'] = df['activation_date'].dt.day
    df["item_seq_bin"] = df["item_seq_number"] // 100
    df["ads_count"] = df.groupby("user_id", as_index=False)["user_id"].transform(lambda s: s.count())

    textfeats = ['description', 'params', 'title']
    for col in textfeats:
        df[col] = df[col].astype(str)
        df[col] = df[col].fillna('NA')  # FILL NA
        df[col] = df[col].str.lower()  # Lowercase all text, so that capitalized words dont get treated differently
        df[col] = df[col].str.replace("[^[:alpha:]]", " ")
        df[col] = df[col].str.replace("\\s+", " ")
        # df[col + '_num_chars'] = df[col].apply(len)
        df[col + '_num_words'] = df[col].apply(lambda s: len(s.split()))
        # df[col + '_num_unique_words'] = df[col].apply(lambda s: len(set(w for w in s.split())))
        # df[col + '_words_vs_unique'] = df[col+'_num_unique_words'] / df[col+'_num_words'] * 100
        df[col + '_num_capE'] = df[col].str.count("[A-Z]")
        df[col + '_num_capR'] = df[col].str.count("[А-Я]")
        df[col + '_num_lowE'] = df[col].str.count("[a-z]")
        df[col + '_num_lowR'] = df[col].str.count("[а-я]")
        df[col + '_num_pun'] = df[col].str.count("[[:punct:]]")
        df[col + '_num_dig'] = df[col].str.count("[[:digit:]]")

    cat_cols = ['region', 'city', 'parent_category_name', 'category_name',
                'param_1', 'param_2', 'param_3', 'user_type', 'image_top_1', "item_seq_bin"]

    # Encoder:
    lbl = preprocessing.LabelEncoder()
    for col in cat_cols:
        df[col], _ = pd.factorize(df[col])

    # print(df.head(5).T)
    X = df[:n_train]
    test = df[n_train:]

    X_train_df, X_val_df = train_test_split(X, shuffle=True, test_size=0.1, random_state=42)


    class FeaturesStatistics():
        def __init__(self, cols):
            self._stats = None
            self._agg_cols = cols

        def fit(self, df):
            '''
            Compute the mean and std of some features from a given data frame
            '''
            self._stats = {}

            # For each feature to be aggregated
            for c in tqdm(self._agg_cols, total=len(self._agg_cols)):
                # Compute the mean and std of the deal prob and the price.
                gp = df.groupby(c)[['deal_probability', 'price']]
                desc = gp.describe()
                self._stats[c] = desc[[('deal_probability', 'mean'), ('deal_probability', 'std'),
                                       ('price', 'mean'), ('price', 'std')]]

        def transform(self, df):
            '''
            Add the mean features statistics computed from another dataset.
            '''
            # For each feature to be aggregated
            for c in tqdm(self._agg_cols, total=len(self._agg_cols)):
                # Add the deal proba and price statistics corrresponding to the feature
                df[c + '_dp_mean'] = df[c].map(self._stats[c][('deal_probability', 'mean')])
                df[c + '_dp_std'] = df[c].map(self._stats[c][('deal_probability', 'std')])
                df[c + '_price_mean'] = df[c].map(self._stats[c][('price', 'mean')])
                df[c + '_price_std'] = df[c].map(self._stats[c][('price', 'std')])

                df[c + '_to_price'] = df.price / df[c + '_price_mean']
                df[c + '_to_price'] = df[c + '_to_price'].fillna(1.0)

        def fit_transform(self, df):
            '''
            First learn the feature statistics, then add them to the dataframe.
            '''
            self.fit(df)
            self.transform(df)


    fStats = FeaturesStatistics(['region', 'city', 'parent_category_name', 'category_name',
                                 'param_1', 'param_2', 'param_3', 'user_type', 'image_top_1', "item_seq_bin"])

    fStats.fit_transform(X_train_df)
    fStats.transform(X_val_df)
    fStats.transform(test)

    ###############################################################################
    print("\n[+] TFIDF features ")

    russian_stop = set(stopwords.words('russian'))

    titles_tfidf = TfidfVectorizer(
        stop_words=russian_stop,
        max_features=6500,
        norm='l2',
        sublinear_tf=True,
        smooth_idf=False,
        dtype=np.float32,
    )

    print("\n[+] Title TFIDF features ")

    train_titles = titles_tfidf.fit_transform(X_train_df.title.astype(str))
    val_titles = titles_tfidf.transform(X_val_df.title.astype(str))
    test_titles = titles_tfidf.transform(test.title.astype(str))

    desc_tfidf = TfidfVectorizer(
        stop_words=russian_stop,
        max_features=6500,
        norm='l2',
        sublinear_tf=True,
        smooth_idf=False,
        dtype=np.float32,
    )

    print("\n[+] Description TFIDF features ")

    train_desc = desc_tfidf.fit_transform(X_train_df.description.astype(str))
    val_desc = desc_tfidf.transform(X_val_df.description.astype(str))
    test_desc = desc_tfidf.transform(test.description.astype(str))

    params_cv = CountVectorizer(
        stop_words=russian_stop,
        max_features=5000,
        dtype=np.float32
    )

    print("\n[+] Params TFIDF features ")

    train_params = params_cv.fit_transform(X_train_df.params.astype(str))
    val_params = params_cv.transform(X_val_df.params.astype(str))
    test_params = params_cv.transform(test.params.astype(str))

    columns_to_drop = ['title', 'description', 'params', 'image',
                       'activation_date', 'deal_probability', 'user_id']

    X_train = hstack([csr_matrix(X_train_df.drop(columns_to_drop, axis=1)), train_titles, train_desc, train_params])
    X_val = hstack([csr_matrix(X_val_df.drop(columns_to_drop, axis=1)), val_titles, val_desc, val_params])
    test = hstack([csr_matrix(test.drop(columns_to_drop, axis=1)), test_titles, test_desc, test_params])

    y_train = X_train_df['deal_probability']
    y_val = X_val_df['deal_probability']

    if nrows is None:
        utils.save_features(X_train, xgb_root, "X_train")
        utils.save_features(X_val, xgb_root, "X_val")
        utils.save_features(test, xgb_root, "test")
        utils.save_features(y_train, xgb_root, "y_train")
        utils.save_features(y_val, xgb_root, "y_val")

elif args.feature == "load":
    print("[+] Load features ")
    X_train = utils.load_features(xgb_root, "X_train").any()
    X_val = utils.load_features(xgb_root, "X_val").any()
    test = utils.load_features(xgb_root, "test").any()
    y_train = utils.load_features(xgb_root, "y_train")
    y_val = utils.load_features(xgb_root, "y_val")
    print("[+] Done ")
    X = vstack([X_train, X_val])
    y = np.concatenate((y_train, y_val))

    X_train, X_val, y_train, y_val = train_test_split(X, y, shuffle=True, test_size=0.1, random_state=42)

    print(y_train)
    print(y_val)


print("Test size {}".format(test.shape[0]))
# assert len_sub != test.shape[0]

# Leave most parameters as default
params = {
    'objective': 'reg:logistic',
    'booster': "gbtree",
    'eval_metric': "rmse",
    # 'tree_method': 'gpu_hist',
    'gpu_id': 0,
    'max_depth': 21,
    'eta': 0.05,
    'min_child_weight': 11,
    'gamma': 0,
    'subsample': 0.85,
    'colsample_bytree': 0.7,
    'silent': True,
    'alpha': 2.0,
    'lambda': 0,
    'nthread': 24,
    # 'max_bin': 16,
    # 'updater': 'grow_gpu',
    # 'tree_method':'exact'
}

xg_train = xgb.DMatrix(X_train, label=y_train)
xg_val = xgb.DMatrix(X_val, label=y_val)
xg_test = xgb.DMatrix(test)
gpu_res = {} # Store accuracy result
watchlist = [(xg_train, 'train'), (xg_val, 'val')]
num_round = 10000
bst = xgb.train(params, xg_train, num_round, evals=watchlist, early_stopping_rounds=200, evals_result=gpu_res, verbose_eval=50)
pred = bst.predict(xg_test)
# print(len(pred))
# sub = pd.read_csv(config["sample_submission"],  nrows=nrows)
sub['deal_probability'] = pred
sub['deal_probability'].clip(0.0, 1.0, inplace=True)
sub.to_csv('submission_xgboost.csv', index=False)