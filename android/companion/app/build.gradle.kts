plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

fun setting(property: String, env: String): String? =
    providers.gradleProperty(property).orElse(providers.environmentVariable(env)).orNull?.takeIf { it.isNotEmpty() }

// Release builds pass -PversionName=X.Y.Z; versionCode follows it as X*10000 + Y*100 + Z.
val versionNameProp: String? = providers.gradleProperty("versionName").orNull
val orbitVersionName = versionNameProp ?: "0.1.0"
val orbitVersionCode = providers.gradleProperty("versionCode").orNull?.let {
    it.toIntOrNull() ?: throw GradleException("versionCode must be an integer, got '$it'")
} ?: versionNameProp?.let { name ->
    val parts = Regex("""(\d+)\.(\d+)\.(\d+)""").matchEntire(name)?.groupValues?.drop(1)?.map { it.toInt() }
        ?: throw GradleException("versionName must be X.Y.Z, got '$name'")
    val (major, minor, patch) = parts
    if (minor > 99 || patch > 99) throw GradleException("versionName $name: minor and patch must be below 100")
    (major * 10000 + minor * 100 + patch).also {
        if (it <= 1) throw GradleException("versionName $name gives versionCode $it, not above the first build's 1")
    }
} ?: 1

val releaseApi = "https://api.github.com/repos/latekvo/orbit/releases/latest"

android {
    namespace = "dev.orbit.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.orbit.companion"
        minSdk = 29
        targetSdk = 35
        versionCode = orbitVersionCode
        versionName = orbitVersionName
    }

    // Signed only when a keystore is given (CI's release workflow); otherwise assembleRelease
    // produces app-release-unsigned.apk.
    val keystore = setting("orbit.keystore", "ORBIT_KEYSTORE")
    signingConfigs {
        if (keystore != null) {
            create("release") {
                storeFile = file(keystore)
                storePassword = setting("orbit.keystorePassword", "ORBIT_KEYSTORE_PASSWORD")
                keyAlias = setting("orbit.keyAlias", "ORBIT_KEY_ALIAS")
                keyPassword = setting("orbit.keyPassword", "ORBIT_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystore != null) signingConfig = signingConfigs.getByName("release")
            buildConfigField("String", "UPDATE_API_URL", "\"$releaseApi\"")
        }
        debug {
            // -PupdateApiUrl points debug builds at a local stand-in for GitHub's API.
            buildConfigField("String", "UPDATE_API_URL", "\"${providers.gradleProperty("updateApiUrl").orNull ?: releaseApi}\"")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    lint {
        abortOnError = true
        warningsAsErrors = false
    }
}

dependencies {
    implementation(project(":protocol"))
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    testImplementation(libs.junit)
}
