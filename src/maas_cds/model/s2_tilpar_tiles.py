"""Custom CDS model definition for s2 tiles"""

import geomet
import logging, opensearchpy
from itertools import groupby
import shapely
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from maas_cds.model.generated import CdsS2Tilpar

LOGGER = logging.getLogger("S2Tiles")


class S2Tiles(CdsS2Tilpar):
    """
    Sentinel-2 TILPAR Tiles mapping
    """

    DLON_THRESHOLD: float = 180.0

    @staticmethod
    def clean_footprint(footprint) -> dict:
        if isinstance(footprint, opensearchpy.AttrDict):
            # GeoShape database convertion
            return footprint.to_dict()

        elif isinstance(footprint, dict):
            # Geojson noconvertion
            return footprint

        elif isinstance(footprint, str):
            # Convert WKT footprint to geojson
            return geomet.wkt.loads(footprint.replace("geography'", "").upper())
        else:
            LOGGER.warning(
                "[FOOTPRINT] - Unhandle footprint format  : %s",
                footprint,
            )

        return None

    @staticmethod
    def intersection(footprint: dict) -> list[str]:
        """
        Get the list of tiles intersected by the given geojson footprint.
        """
        assert footprint

        if not S2Tiles.is_crossing_antemeridian(footprint):
            return S2Tiles.search_intersecting_tiles(
                S2Tiles.remove_duplicate_points(footprint)
            )

        corrected_geojson = S2Tiles.correct_polygon(footprint)
        try:
            return S2Tiles.search_intersecting_tiles(corrected_geojson)
        except opensearchpy.exceptions.RequestError as ex:
            # Never fall back on the original footprint: across the antemeridian it is
            # understood as a band around the globe and matches thousands of tiles.
            # The bounding boxes over-estimate the tiles but stay local.
            LOGGER.error(
                "Error requesting intersection between S2Tiles and corrected polygon "
                "(%s)! Requesting with its bounding boxes.",
                ex,
            )
            return S2Tiles.search_intersecting_tiles(
                S2Tiles.bounding_boxes(corrected_geojson)
            )

    @staticmethod
    def search_intersecting_tiles(geojson: dict) -> list[str]:
        """
        Get the name of the tiles intersecting the given geojson geometry.
        """
        tiles_search = (
            S2Tiles.search()
            .filter(
                "geo_shape",
                geometry={
                    "relation": "intersects",
                    "shape": geojson,
                },
            )
            .scan()
        )
        return [tile.name for tile in tiles_search]

    @staticmethod
    def remove_duplicate_points(footprint: dict) -> dict:
        """
        Remove duplicated points from geojson geometry.
        """
        footprint["coordinates"] = [[p[0] for p in groupby(*footprint["coordinates"])]]

        return footprint

    @staticmethod
    def is_crossing_antemeridian(geojson_footprint: dict):
        """
        Check if geojson polygon cross antemeridian
        """
        for coordinates in geojson_footprint["coordinates"]:
            nbredges = len(coordinates)
            for edge_nbr in range(0, nbredges):
                if edge_nbr < nbredges - 1:
                    lon1 = float(coordinates[edge_nbr][0])
                    lon2 = float(coordinates[edge_nbr + 1][0])
                    if abs(lon2 - lon1) > S2Tiles.DLON_THRESHOLD:
                        LOGGER.info(
                            "Segment is crossing antemeridian [%s,%s], distance %s  > %s !!",
                            str(lon1),
                            str(lon2),
                            str(abs(lon2 - lon1)),
                            S2Tiles.DLON_THRESHOLD,
                        )
                        return True
        return False

    @staticmethod
    def correct_polygon(geojson_footprint: dict) -> dict:
        """
        Cut a polygon crossing the antemeridian into a geojson multipolygon
        whose parts stay within [-180, 180] (RFC 7946 section 3.1.9)
        """
        # Shift the western longitudes by 360° to get a continuous polygon over [0, 360]
        shell, *holes = [
            [
                (float(lon) + 360 if float(lon) < 0 else float(lon), float(lat))
                for lon, lat, *_ in ring
            ]
            for ring in geojson_footprint["coordinates"]
        ]
        unwrapped = Polygon(shell, holes)
        if not unwrapped.is_valid:
            unwrapped = unwrapped.buffer(0)

        eastern = unwrapped.intersection(box(0, -90, 180, 90))
        western = translate(unwrapped.intersection(box(180, -90, 360, 90)), xoff=-360)

        return mapping(
            MultiPolygon(
                [
                    part
                    for part in shapely.get_parts([eastern, western])
                    if isinstance(part, Polygon) and not part.is_empty
                ]
            )
        )

    @staticmethod
    def bounding_boxes(geojson: dict) -> dict:
        """
        Get the bounding box of each part of a geojson geometry as a multipolygon
        """
        return mapping(
            MultiPolygon([part.envelope for part in shapely.get_parts(shape(geojson))])
        )
