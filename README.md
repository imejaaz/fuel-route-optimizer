# Fuel Route Optimizer

Django API that returns a US driving route, cost-conscious fuel stops, and an
estimated trip fuel cost. Routing uses the public OpenStreetMap Nominatim
geocoder and OSRM routing service. Fuel prices are read from the supplied
`fuel-prices-for-be-assessment.csv`; the API makes two geocoding requests and
one routing request per uncached route.

## Setup

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

## API

`POST /api/route/` with a JSON body:

```json
{
  "start": "Seattle, WA",
  "finish": "Portland, OR"
}
```

The response contains a GeoJSON `LineString`, route distance, estimated fuel
cost, and fuel stops with their price and distance along the route. `start` and
`finish` must resolve to locations in the United States. The estimate assumes
10 MPG, a 500-mile maximum range, and that the trip's fuel is priced using the
recommended stop prices (the first stop's price is used for the initial leg).
Stops are optimized from the supplied dataset.

The CSV provides city/state but not station coordinates. Fuel stop map points
therefore use approximate Census place centroids from the bundled 2025
Gazetteer dataset, and only stations within 30 straight-line miles of the route
are considered. The fuel price records are collapsed to the lowest listed
price per city/state. The coordinate data comes from the US Census Bureau's
[2025 Gazetteer files](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html).

Run the focused tests with:

```sh
python manage.py test core
```