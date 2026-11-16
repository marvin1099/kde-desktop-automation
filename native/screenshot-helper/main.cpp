#include <QByteArray>
#include <QCoreApplication>
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusUnixFileDescriptor>
#include <QImage>
#include <QImageWriter>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSaveFile>
#include <QVariantMap>
#include <QUuid>

#include <cerrno>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <poll.h>
#include <unistd.h>

namespace
{
constexpr auto service = "org.kde.KWin.ScreenShot2";
constexpr auto objectPath = "/org/kde/KWin/ScreenShot2";
constexpr auto interfaceName = "org.kde.KWin.ScreenShot2";
constexpr int dbusTimeoutMs = 30000;
constexpr int pipeTimeoutMs = 30000;
constexpr quint64 maxPixels = 16'777'216;
constexpr quint64 maxRawBytes = 256 * 1024 * 1024;

struct CaptureRequest {
    QString mode;
    QString windowId;
    QString output;
};

void reportError(const QString &message)
{
    std::cerr << message.toStdString() << '\n';
}

bool parseRequest(int argc, char **argv, CaptureRequest *request)
{
    if (argc < 4) {
        reportError(QStringLiteral(
            "Usage: kde-desktop-screenshot capture-workspace --output PATH\n"
            "       kde-desktop-screenshot capture-window --window-id UUID --output PATH"));
        return false;
    }

    request->mode = QString::fromLocal8Bit(argv[1]);
    if (request->mode != QStringLiteral("capture-workspace")
        && request->mode != QStringLiteral("capture-window")) {
        reportError(QStringLiteral("Unknown screenshot operation."));
        return false;
    }

    for (int index = 2; index < argc; index += 2) {
        if (index + 1 >= argc) {
            reportError(QStringLiteral("Every option must have a value."));
            return false;
        }
        const QString option = QString::fromLocal8Bit(argv[index]);
        const QString value = QString::fromLocal8Bit(argv[index + 1]);
        if (option == QStringLiteral("--output")) {
            request->output = value;
        } else if (option == QStringLiteral("--window-id")) {
            request->windowId = value;
        } else {
            reportError(QStringLiteral("Unknown screenshot option: %1").arg(option));
            return false;
        }
    }

    if (request->output.isEmpty()) {
        reportError(QStringLiteral("An output path is required."));
        return false;
    }
    if (request->mode == QStringLiteral("capture-window")) {
        if (QUuid(request->windowId).isNull()) {
            reportError(QStringLiteral("A valid KWin window UUID is required."));
            return false;
        }
    } else if (!request->windowId.isEmpty()) {
        reportError(QStringLiteral("--window-id is valid only for capture-window."));
        return false;
    }
    return true;
}

bool readImagePipe(int fileDescriptor, quint64 byteCount, QByteArray *bytes, QString *error)
{
    if (byteCount > maxRawBytes || byteCount > static_cast<quint64>(std::numeric_limits<int>::max())) {
        *error = QStringLiteral("KWin returned an image that exceeds the helper's size limit.");
        return false;
    }

    bytes->resize(static_cast<int>(byteCount));
    quint64 offset = 0;
    while (offset < byteCount) {
        pollfd descriptor{fileDescriptor, POLLIN, 0};
        const int ready = poll(&descriptor, 1, pipeTimeoutMs);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            *error = QStringLiteral("Could not wait for KWin screenshot data: %1")
                         .arg(QString::fromLocal8Bit(std::strerror(errno)));
            return false;
        }
        if (ready == 0) {
            *error = QStringLiteral("KWin did not finish writing the screenshot in time.");
            return false;
        }
        const ssize_t count = read(fileDescriptor,
                                   bytes->data() + offset,
                                   static_cast<size_t>(byteCount - offset));
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN) {
                continue;
            }
            *error = QStringLiteral("Could not read KWin screenshot data: %1")
                         .arg(QString::fromLocal8Bit(std::strerror(errno)));
            return false;
        }
        if (count == 0) {
            *error = QStringLiteral("KWin closed the screenshot pipe before the image was complete.");
            return false;
        }
        offset += static_cast<quint64>(count);
    }
    return true;
}

bool capture(const CaptureRequest &request, QJsonObject *result)
{
    int pipeDescriptors[2];
    if (pipe2(pipeDescriptors, O_CLOEXEC) != 0) {
        reportError(QStringLiteral("Could not create a private screenshot pipe: %1")
                        .arg(QString::fromLocal8Bit(std::strerror(errno))));
        return false;
    }

    const bool windowCapture = request.mode == QStringLiteral("capture-window");
    const QString method = windowCapture ? QStringLiteral("CaptureWindow")
                                         : QStringLiteral("CaptureWorkspace");
    QVariantMap options{
        {QStringLiteral("native-resolution"), true},
    };
    if (windowCapture) {
        options.insert(QStringLiteral("include-decoration"), true);
        options.insert(QStringLiteral("include-shadow"), true);
    }

    QDBusMessage message = QDBusMessage::createMethodCall(
        QString::fromLatin1(service),
        QString::fromLatin1(objectPath),
        QString::fromLatin1(interfaceName),
        method);
    if (windowCapture) {
        message << request.windowId;
    }
    message << QVariant::fromValue(options)
            << QVariant::fromValue(QDBusUnixFileDescriptor(pipeDescriptors[1]));

    const QDBusMessage reply = QDBusConnection::sessionBus().call(
        message,
        QDBus::Block,
        dbusTimeoutMs);
    close(pipeDescriptors[1]);

    if (reply.type() != QDBusMessage::ReplyMessage || reply.arguments().isEmpty()) {
        close(pipeDescriptors[0]);
        const QString error = reply.errorMessage().isEmpty()
            ? QStringLiteral("KWin Screenshot2 returned no metadata.")
            : QStringLiteral("KWin Screenshot2 failed: %1 (%2)")
                  .arg(reply.errorName(), reply.errorMessage());
        reportError(error);
        return false;
    }

    const QVariant resultValue = reply.arguments().first();
    const QVariantMap metadata = resultValue.metaType() == QMetaType::fromType<QDBusArgument>()
        ? qdbus_cast<QVariantMap>(resultValue.value<QDBusArgument>())
        : resultValue.toMap();
    const QString type = metadata.value(QStringLiteral("type")).toString();
    const quint64 width = metadata.value(QStringLiteral("width")).toULongLong();
    const quint64 height = metadata.value(QStringLiteral("height")).toULongLong();
    const quint64 stride = metadata.value(QStringLiteral("stride")).toULongLong();
    const uint formatValue = metadata.value(QStringLiteral("format")).toUInt();
    const double scale = metadata.value(QStringLiteral("scale")).toDouble();
    if (type != QStringLiteral("raw") || width == 0 || height == 0
        || width > std::numeric_limits<int>::max() || height > std::numeric_limits<int>::max()
        || width * height > maxPixels || stride == 0 || stride > maxRawBytes
        || height > maxRawBytes / stride || !std::isfinite(scale) || scale <= 0) {
        close(pipeDescriptors[0]);
        reportError(QStringLiteral(
                        "KWin Screenshot2 returned invalid image metadata "
                        "(type=%1, width=%2, height=%3, stride=%4, format=%5, scale=%6).")
                        .arg(type)
                        .arg(width)
                        .arg(height)
                        .arg(stride)
                        .arg(formatValue)
                        .arg(scale));
        return false;
    }

    const auto imageFormat = static_cast<QImage::Format>(formatValue);
    const QImage formatProbe(1, 1, imageFormat);
    if (formatProbe.isNull()
        || stride < (width * static_cast<quint64>(formatProbe.depth()) + 7) / 8) {
        close(pipeDescriptors[0]);
        reportError(QStringLiteral("KWin Screenshot2 returned an unsupported raw image format."));
        return false;
    }

    QByteArray pixelBytes;
    QString pipeError;
    const bool pipeRead = readImagePipe(pipeDescriptors[0], height * stride, &pixelBytes, &pipeError);
    close(pipeDescriptors[0]);
    if (!pipeRead) {
        reportError(pipeError);
        return false;
    }

    const QImage wrapped(reinterpret_cast<const uchar *>(pixelBytes.constData()),
                         static_cast<int>(width),
                         static_cast<int>(height),
                         static_cast<qsizetype>(stride),
                         imageFormat);
    if (wrapped.isNull()) {
        reportError(QStringLiteral("Could not construct an image from KWin's raw pixels."));
        return false;
    }
    const QImage image = wrapped.copy();

    QSaveFile output(request.output);
    if (!output.open(QIODevice::WriteOnly)) {
        reportError(QStringLiteral("Could not open the screenshot output: %1")
                        .arg(output.errorString()));
        return false;
    }
    QImageWriter writer(&output, "png");
    if (!writer.write(image)) {
        output.cancelWriting();
        reportError(QStringLiteral("Could not encode the KWin image as PNG: %1")
                        .arg(writer.errorString()));
        return false;
    }
    if (!output.commit()) {
        reportError(QStringLiteral("Could not commit the PNG output: %1")
                        .arg(output.errorString()));
        return false;
    }

    result->insert(QStringLiteral("width"), static_cast<qint64>(width));
    result->insert(QStringLiteral("height"), static_cast<qint64>(height));
    result->insert(QStringLiteral("scale"), scale);
    if (windowCapture) {
        const QString capturedWindowId = metadata.value(QStringLiteral("windowId")).toString();
        if (!capturedWindowId.isEmpty() && QUuid(capturedWindowId) != QUuid(request.windowId)) {
            reportError(QStringLiteral("KWin returned a different window UUID than requested."));
            return false;
        }
        result->insert(QStringLiteral("window_id"), request.windowId);
    }
    return true;
}
} // namespace

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    CaptureRequest request;
    if (!parseRequest(argc, argv, &request)) {
        return 2;
    }

    QJsonObject metadata;
    if (!capture(request, &metadata)) {
        return 1;
    }
    std::cout << QJsonDocument(metadata).toJson(QJsonDocument::Compact).constData() << '\n';
    return 0;
}
