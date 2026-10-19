package dev.orbit.companion

import org.junit.Assert.assertEquals
import org.junit.Test

class BackgroundTest {
    @Test
    fun oemBrands() {
        val cases = mapOf(
            "OPPO" to "OPPO", "realme" to "realme", "OnePlus" to "OnePlus", "Xiaomi" to "Xiaomi", "Redmi" to "Xiaomi",
            "POCO" to "Xiaomi", "HUAWEI" to "Huawei", "HONOR" to "Huawei", "samsung" to "Samsung",
            "Google" to null, "motorola" to null, "" to null,
        )
        for ((manufacturer, brand) in cases) assertEquals(manufacturer, brand, Background.oemBrand(manufacturer))
    }
}
