"""Generate the committed CourtListener court map from a local CSV.

The source object has the form
``https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/courts-YYYY-MM-DD.csv.bz2``.
This package never fetches it; the command accepts only a local path supplied by
the person regenerating the release artifact.
"""
