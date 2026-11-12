plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "dev.freehold.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.freehold.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        ndk {
            // Chaquopy needs an explicit ABI list. Both current phone
            // architectures; drop one to halve the APK.
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }

        python {
            version = "3.11"
            pip {
                // The one runtime dependency. Chaquopy ships a prebuilt
                // wheel for it; if a future version is missing, pin here.
                install("cryptography")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { viewBinding = true }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
}

// The Python half of this app is the same freehold package the desktop
// uses, copied in at build time rather than duplicated in the repository.
// One implementation of chunking, dedup and encryption, as intended.
val syncPythonCore by tasks.registering(Copy::class) {
    from(rootProject.file("../freehold")) {
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    into(layout.buildDirectory.dir("python-core/freehold"))
}

android.sourceSets.getByName("main") {
    // Chaquopy reads Python sources from here.
    setProperty("python.srcDirs", listOf(
        layout.buildDirectory.dir("python-core").get().asFile.path,
        "src/main/python"
    ))
}

tasks.matching { it.name.startsWith("generate") || it.name.contains("Python") }
    .configureEach { dependsOn(syncPythonCore) }
tasks.named("preBuild") { dependsOn(syncPythonCore) }
