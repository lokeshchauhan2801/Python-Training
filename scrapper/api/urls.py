"""URLConf for the scrapper API, mounted at ``api/v1/scrapper/``."""

from rest_framework.routers import SimpleRouter

from .views import ScrapeJobViewSet

router = SimpleRouter(trailing_slash=True)
router.register("scrape-job", ScrapeJobViewSet, basename="scrape-job")

urlpatterns = router.urls
