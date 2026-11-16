#include <QBackingStore>
#include <QGuiApplication>
#include <QPainter>
#include <QRegion>
#include <QWindow>

class ScreenshotTestWindow final : public QWindow
{
public:
    explicit ScreenshotTestWindow(const QString &title)
        : m_backingStore(this)
    {
        setTitle(title);
        resize(640, 480);
    }

protected:
    void exposeEvent(QExposeEvent *event) override
    {
        QWindow::exposeEvent(event);
        render();
    }

    void resizeEvent(QResizeEvent *event) override
    {
        QWindow::resizeEvent(event);
        render();
    }

private:
    void render()
    {
        if (!isExposed() || size().isEmpty()) {
            return;
        }
        const QRect bounds(QPoint(0, 0), size());
        const QRegion region(bounds);
        m_backingStore.resize(size());
        m_backingStore.beginPaint(region);
        QPainter painter(m_backingStore.paintDevice());
        painter.fillRect(bounds, QColor(35, 92, 160));
        painter.setPen(Qt::white);
        painter.drawText(bounds, Qt::AlignCenter, title());
        painter.end();
        m_backingStore.endPaint();
        m_backingStore.flush(region);
    }

    QBackingStore m_backingStore;
};

int main(int argc, char **argv)
{
    QGuiApplication application(argc, argv);
    const QString title = argc > 1
        ? QString::fromLocal8Bit(argv[1])
        : QStringLiteral("KWin Screenshot2 Integration Window");
    ScreenshotTestWindow window(title);
    window.show();
    return application.exec();
}
