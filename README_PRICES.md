# Energy Management: Supported Price Formats

To use the dynamic pricing features of this integration, you need to provide sensors that contain hourly array data for today's (and optionally tomorrow's) energy prices.

## Required Setup
In the Integration Configuration (via Home Assistant UI), you will see two fields:
- **Tariff: Grid Buying Price**
- **Tariff: Grid Selling Price**

You can provide a **single sensor** or a **list of sensors** for each. If a list is provided, the integration will aggregate price data from all sources (useful for combining multiple regions or providers).

## Supported Sensor Format
The integration's *"Universal Price Extractor"* is designed to parse the popular formats used by European energy exchanges (like Nordpool, ENTSO-E, Octopuss, etc.).

Your sensor's **attributes** must contain one of the following keys:
`price_today`, `prices_today`, `prices`, `data`, `raw_today`, `price_tomorrow`, `prices_tomorrow`, or `raw_tomorrow`.

Inside this attribute, there must be a list containing hourly time and price objects.

### Example Valid Format (YAML representation of Attributes):
```yaml
price_today:
  - start: '2026-03-08T00:00:00+01:00'
    end: '2026-03-08T01:00:00+01:00'
    price: 0.94
  - start: '2026-03-08T01:00:00+01:00'
    end: '2026-03-08T02:00:00+01:00'
    price: 0.91
  ...
price_tomorrow:
  - start: '2026-03-09T00:00:00+01:00'
    ...
```

### Key Variations Supported:
- **Time identifiers:** `start`, `start_time`, `time`, `datetime`
- **Price identifiers:** `price`, `value`, `total`

If your provider integration splits Today and Tomorrow into two separate attributes (e.g. `price_today` and `price_tomorrow`) within the **same sensor**, our Universal Extractor will automatically detect both arrays and merge them into the internal 48-hour routing cache.
