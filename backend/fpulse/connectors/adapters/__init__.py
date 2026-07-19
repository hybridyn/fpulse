"""Generic protocol adapters.

These modules wrap the v1 manifest runtime (rest_framework) with leaner,
config-driven entry points so a future first-class connector class can
delegate execution without authoring a full manifest file.

  * ``odata`` — v2 (d.results + d.__next) / v4 (value + @odata.nextLink)
    OData engine. Used by sap_s4hana / sap_successfactors / odata.
  * ``rest``  — generic REST engine. Centralises the pagination styles
    the rest_framework supports (cursor, url, link_header, offset_limit,
    page_number) so adapter callers don't have to re-think the pagination
    config shape per connector.

Both modules are read-only wrappers over rest_framework; T1's runtime
fixes (default_query merge, url-pagination) apply automatically.
"""
